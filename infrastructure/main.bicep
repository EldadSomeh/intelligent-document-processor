// ============================================================================
// Intelligent Document Processor – Infrastructure (Bicep)
//
// Deploys:
//   • Storage Account (no public access) with containers: raw, artifacts, outputs
//   • Private Endpoints + DNS Zones for blob, queue, table, file
//   • Azure Container Registry (Docker image store)
//   • App Service Plan (Linux) + Function App (Docker, Python 3.11)
//   • Durable Functions orchestration (blob trigger → preprocess → OCR → summarize)
//   • Application Insights
//   • VNet with integration, private-endpoint, and appgw subnets
//   • Application Gateway v2 (public entry point, injects func key)
//   • User-Assigned Identity + Deployment Script to auto-build Docker image
//   • Role assignments: Blob Data Owner, Queue Data Contributor,
//     Table Data Contributor for the Function App managed identity
// ============================================================================

// ── Parameters ──────────────────────────────────────────────────────────────

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short project prefix (lowercase, no hyphens).')
@minLength(3)
@maxLength(8)
param projectName string = 'preocr'

@description('Environment tag (dev | staging | prod).')
@allowed(['dev', 'staging', 'prod'])
param env string = 'dev'

@description('Docker image name (without registry prefix).')
param imageName string = 'preocr-func'

@description('Docker image tag.')
param imageTag string = 'latest'

@description('GitHub repo URL for ACR build. For private repos, use a PAT: https://<TOKEN>@github.com/owner/repo.git#branch:folder')
param sourceRepoUrl string = 'https://github.com/EldadSomeh/intelligent-document-processor.git#main:function-app'

@description('Optional: existing VNet resource ID. Leave empty to create a new VNet.')
param existingVnetId string = ''

@description('VNet address space (used only when creating a new VNet).')
param vnetAddressPrefix string = '10.0.0.0/16'

@description('Subnet for VNet-integrated apps (Function App).')
param integrationSubnetPrefix string = '10.0.1.0/24'

@description('Subnet for private endpoints.')
param privateEndpointSubnetPrefix string = '10.0.2.0/24'

@description('Subnet for Application Gateway.')
param appGwSubnetPrefix string = '10.0.3.0/24'

@description('App Service Plan SKU for the Function App. Use P1v3 (default) for best availability, or B1 for lower cost.')
param funcPlanSku string = 'P1v3'

@description('App Service Plan tier for the Function App (PremiumV3, Basic, Standard).')
param funcPlanTier string = 'PremiumV3'

@secure()
@description('Function App host key – injected by AG as x-functions-key header.')
param functionHostKey string = ''

// ── Variables ───────────────────────────────────────────────────────────────

var suffix                = take(uniqueString(resourceGroup().id), 6)
var storageAccountName    = '${projectName}${env}${suffix}'
var acrName               = '${projectName}${env}${suffix}'
var appInsightsName       = '${projectName}-${env}-ai-${suffix}'
var funcPlanName          = '${projectName}-${env}-func-plan'
var funcAppName           = '${projectName}-${env}-func-${suffix}'
var buildScriptName       = '${projectName}-${env}-build-image'
var buildIdentityName     = '${projectName}-${env}-build-id'
var vnetName              = '${projectName}-${env}-vnet-${suffix}'

// Pre-generated self-signed PFX (CN=placeholder.local, 10-yr expiry, password=TemplateCert1!).
// Replace with your own certificate for production / custom domain.
var placeholderCertPfx    = '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'
var integrationSubnetName = 'snet-integration'
var peSubnetName          = 'snet-private-endpoints'
var appGwSubnetName       = 'snet-appgw'
var appGwName             = '${projectName}-${env}-appgw'
var appGwPipName          = '${projectName}-${env}-appgw-pip'
var appGwPipDnsLabel      = '${projectName}-${env}-${suffix}'

// Well-known role definition IDs
var storageBlobDataOwner         = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributor  = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributor  = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

// ── Storage Account ─────────────────────────────────────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true             // Required for Durable Functions internal storage
    publicNetworkAccess: 'Disabled'        // ← survives Azure Policy
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource rawContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'raw'
}

resource artifactsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
}

resource outputsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'outputs'
}

// ── VNet ────────────────────────────────────────────────────────────────────
// Creates a new VNet if existingVnetId is empty; otherwise reference existing.

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (empty(existingVnetId)) {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ vnetAddressPrefix ] }
    subnets: [
      {
        name: integrationSubnetName
        properties: {
          addressPrefix: integrationSubnetPrefix
          delegations: [
            {
              name: 'delegation-serverfarms'
              properties: { serviceName: 'Microsoft.Web/serverFarms' }
            }
          ]
        }
      }
      {
        name: peSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: appGwSubnetName
        properties: {
          addressPrefix: appGwSubnetPrefix
        }
      }
    ]
  }
}

var effectiveVnetId          = empty(existingVnetId) ? vnet.id : existingVnetId
var integrationSubnetId      = '${effectiveVnetId}/subnets/${integrationSubnetName}'
var privateEndpointSubnetId  = '${effectiveVnetId}/subnets/${peSubnetName}'
var appGwSubnetId            = '${effectiveVnetId}/subnets/${appGwSubnetName}'

// ── Private DNS Zones ───────────────────────────────────────────────────────

var privateDnsZones = [
  'privatelink.blob.${az.environment().suffixes.storage}'
  'privatelink.queue.${az.environment().suffixes.storage}'
  'privatelink.table.${az.environment().suffixes.storage}'
  'privatelink.file.${az.environment().suffixes.storage}'
]

resource dnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zone in privateDnsZones: {
  name: zone
  location: 'global'
}]

resource dnsVnetLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zone, i) in privateDnsZones: {
  parent: dnsZones[i]
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: effectiveVnetId }
    registrationEnabled: false
  }
}]

// ── Private Endpoints (blob, queue, table, file) ─────────────────────────────

var peSubResources = [
  { name: 'blob', groupId: 'blob', dnsIdx: 0 }
  { name: 'queue', groupId: 'queue', dnsIdx: 1 }
  { name: 'table', groupId: 'table', dnsIdx: 2 }
  { name: 'file', groupId: 'file', dnsIdx: 3 }
]

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2023-11-01' = [for pe in peSubResources: {
  name: '${storageAccountName}-pe-${pe.name}'
  location: location
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: '${storageAccountName}-${pe.name}'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: [ pe.groupId ]
        }
      }
    ]
  }
}]

resource peDnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = [for (pe, i) in peSubResources: {
  parent: privateEndpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'config-${pe.name}'
        properties: {
          privateDnsZoneId: dnsZones[pe.dnsIdx].id
        }
      }
    ]
  }
}]

// ── Azure Container Registry ────────────────────────────────────────────────

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

// ── Application Insights ────────────────────────────────────────────────────

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    RetentionInDays: 30
  }
}

// ── Function App (Docker on Linux) ──────────────────────────────────────────

resource funcPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: funcPlanName
  location: location
  kind: 'linux'
  sku: { name: funcPlanSku, tier: funcPlanTier }
  properties: { reserved: true }
}

resource funcApp 'Microsoft.Web/sites@2023-12-01' = {
  name: funcAppName
  location: location
  kind: 'functionapp,linux,container'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: funcPlan.id
    virtualNetworkSubnetId: integrationSubnetId
    vnetRouteAllEnabled: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${imageName}:${imageTag}'
      appSettings: [
        // Connection-string AzureWebJobsStorage — avoids Azure AD role-propagation
        // delay that causes 403 on fresh deployments when using identity-based auth.
        { name: 'AzureWebJobsStorage',            value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${az.environment().suffixes.storage}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION',    value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',       value: 'python' }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'STORAGE_ACCOUNT_URL',            value: storageAccount.properties.primaryEndpoints.blob }
        { name: 'STORAGE_ACCOUNT_KEY',            value: storageAccount.listKeys().keys[0].value }
        { name: 'DOCKER_REGISTRY_SERVER_URL',     value: 'https://${acr.properties.loginServer}' }
        { name: 'DOCKER_REGISTRY_SERVER_USERNAME', value: acr.listCredentials().username }
        { name: 'DOCKER_REGISTRY_SERVER_PASSWORD', value: acr.listCredentials().passwords[0].value }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'WEBSITE_VNET_ROUTE_ALL',         value: '1' }
        { name: 'WEBSITE_DNS_SERVER',             value: '168.63.129.16' }
      ]
    }
  }
}

// ── Role Assignments ────────────────────────────────────────────────────────
// Function App needs:
//   • Storage Blob Data Owner        – runtime manages lease blobs
//   • Storage Queue Data Contributor – runtime uses internal queues
//   • Storage Table Data Contributor – Durable Functions uses tables
// These are required when using identity-based AzureWebJobsStorage.

// -- Function App roles --
resource funcBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, funcApp.id, storageBlobDataOwner)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: funcApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, funcApp.id, storageQueueDataContributor)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributor)
    principalId: funcApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, funcApp.id, storageTableDataContributor)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributor)
    principalId: funcApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Application Gateway v2 ──────────────────────────────────────────────────
// Public entry-point: TLS termination (embedded self-signed placeholder cert),
// HTTP→HTTPS 301 redirect, root "/" → /api/ui rewrite, and x-functions-key
// header injection so callers do not need ?code= in the URL.
// Replace placeholderCertPfx with your own certificate for production.

resource appGwPip 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: appGwPipName
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: { domainNameLabel: appGwPipDnsLabel }
  }
}

resource appGw 'Microsoft.Network/applicationGateways@2023-11-01' = {
  name: appGwName
  location: location
  properties: {
    sku: {
      name: 'Standard_v2'
      tier: 'Standard_v2'
      capacity: 1
    }
    gatewayIPConfigurations: [
      {
        name: 'appGatewayIpConfig'
        properties: { subnet: { id: appGwSubnetId } }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'appGwFrontendIP'
        properties: { publicIPAddress: { id: appGwPip.id } }
      }
    ]
    frontendPorts: [
      { name: 'port80',  properties: { port: 80 } }
      { name: 'port443', properties: { port: 443 } }
    ]
    sslCertificates: [
      {
        name: 'appGwSslCert'
        properties: {
          data: placeholderCertPfx
          password: 'TemplateCert1!'
        }
      }
    ]
    backendAddressPools: [
      {
        name: 'funcBackendPool'
        properties: {
          backendAddresses: [
            { fqdn: funcApp.properties.defaultHostName }
          ]
        }
      }
    ]
    probes: [
      {
        name: 'funcHealthProbe'
        properties: {
          protocol: 'Https'
          host: funcApp.properties.defaultHostName
          path: '/'
          interval: 30
          timeout: 30
          unhealthyThreshold: 3
          pickHostNameFromBackendHttpSettings: false
          match: { statusCodes: [ '200-401' ] }
        }
      }
    ]
    backendHttpSettingsCollection: [
      {
        name: 'funcHttpSettings'
        properties: {
          port: 443
          protocol: 'Https'
          pickHostNameFromBackendAddress: true
          requestTimeout: 60
          probe: { id: resourceId('Microsoft.Network/applicationGateways/probes', appGwName, 'funcHealthProbe') }
        }
      }
    ]
    rewriteRuleSets: [
      {
        name: 'mainRewriteRules'
        properties: {
          rewriteRules: concat([
            {
              name: 'rootToUi'
              ruleSequence: 50
              conditions: [
                {
                  variable: 'var_uri_path'
                  pattern: '^/$'
                  ignoreCase: true
                  negate: false
                }
              ]
              actionSet: {
                urlConfiguration: {
                  modifiedPath: '/api/ui'
                  reroute: false
                }
              }
            }
          ], !empty(functionHostKey) ? [
            {
              name: 'addFuncKeyHeader'
              ruleSequence: 100
              actionSet: {
                requestHeaderConfigurations: [
                  { headerName: 'x-functions-key', headerValue: functionHostKey }
                ]
              }
            }
          ] : [])
        }
      }
    ]
    redirectConfigurations: [
      {
        name: 'httpToHttpsRedirect'
        properties: {
          redirectType: 'Permanent'
          targetListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'httpsListener') }
          includePath: true
          includeQueryString: true
        }
      }
    ]
    httpListeners: [
      {
        name: 'httpListener'
        properties: {
          frontendIPConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appGwFrontendIP') }
          frontendPort: { id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'port80') }
          protocol: 'Http'
        }
      }
      {
        name: 'httpsListener'
        properties: {
          frontendIPConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appGwFrontendIP') }
          frontendPort: { id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'port443') }
          protocol: 'Https'
          sslCertificate: { id: resourceId('Microsoft.Network/applicationGateways/sslCertificates', appGwName, 'appGwSslCert') }
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'httpRedirectRule'
        properties: {
          priority: 100
          ruleType: 'Basic'
          httpListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'httpListener') }
          redirectConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/redirectConfigurations', appGwName, 'httpToHttpsRedirect') }
        }
      }
      {
        name: 'httpsToFuncRule'
        properties: {
          priority: 200
          ruleType: 'Basic'
          httpListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'httpsListener') }
          backendAddressPool: { id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'funcBackendPool') }
          backendHttpSettings: { id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', appGwName, 'funcHttpSettings') }
          rewriteRuleSet: { id: resourceId('Microsoft.Network/applicationGateways/rewriteRuleSets', appGwName, 'mainRewriteRules') }
        }
      }
    ]
  }
}

// ── Managed Identity for Deployment Script ──────────────────────────────────
// A user-assigned identity that has AcrPush + Contributor rights so the
// deploymentScript can run `az acr build` and restart the Function App.

resource buildIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: buildIdentityName
  location: location
}

// AcrPush (8311e382-...) on the container registry
resource buildAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, buildIdentity.id, '8311e382-0749-4cb8-b61a-304f252e45ec')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
    principalId: buildIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Contributor on the resource group so the script can restart the Function App
resource buildContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, buildIdentity.id, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
    principalId: buildIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Deployment Script: Build Docker Image & Deploy ──────────────────────────
// Clones the GitHub repo, builds the Docker image in ACR, then restarts the
// Function App so it pulls the new image.

resource buildScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: buildScriptName
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${buildIdentity.id}': {}
    }
  }
  properties: {
    azCliVersion: '2.63.0'
    timeout: 'PT30M'
    retentionInterval: 'P1D'
    environmentVariables: [
      { name: 'ACR_NAME',       value: acr.name }
      { name: 'IMAGE_NAME',     value: imageName }
      { name: 'IMAGE_TAG',      value: imageTag }
      { name: 'SOURCE_REPO',    value: sourceRepoUrl }
      { name: 'RG_NAME',        value: resourceGroup().name }
      { name: 'FUNC_APP_NAME',  value: funcApp.name }
    ]
    scriptContent: '''
      echo "Building Docker image in ACR..."
      az acr build --registry $ACR_NAME --image $IMAGE_NAME:$IMAGE_TAG $SOURCE_REPO --no-logs 2>&1 || {
        echo "ACR build failed, retrying..."
        sleep 10
        az acr build --registry $ACR_NAME --image $IMAGE_NAME:$IMAGE_TAG $SOURCE_REPO 2>&1
      }
      echo "Restarting Function App to pull new image..."
      az functionapp restart --resource-group $RG_NAME --name $FUNC_APP_NAME 2>&1
      echo "Done!"
    '''
  }
  dependsOn: [
    buildAcrPush
    buildContributor
  ]
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output storageAccountName    string = storageAccount.name
output storageAccountBlobUrl string = storageAccount.properties.primaryEndpoints.blob
output acrLoginServer        string = acr.properties.loginServer
output functionAppHostname   string = funcApp.properties.defaultHostName
output appGwPublicFqdn       string = appGwPip.properties.dnsSettings.fqdn
