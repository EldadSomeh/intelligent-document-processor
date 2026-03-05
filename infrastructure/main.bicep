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

@description('GitHub repo URL for ACR build (includes Dockerfile).')
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

@description('App Service Plan SKU for the Function App (B1, B2, B3, S1, P1v3, etc.).')
param funcPlanSku string = 'B1'

@description('App Service Plan tier for the Function App.')
param funcPlanTier string = 'Basic'

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
        // Identity-based AzureWebJobsStorage (no keys!)
        { name: 'AzureWebJobsStorage__accountName',  value: storageAccount.name }
        { name: 'FUNCTIONS_EXTENSION_VERSION',    value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',       value: 'python' }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'STORAGE_ACCOUNT_URL',            value: storageAccount.properties.primaryEndpoints.blob }
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
// Public entry-point for the Function App UI & API.
// Injects the Function host key via x-functions-key header so callers do not
// need to supply ?code= in the URL.

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
      { name: 'port80', properties: { port: 80 } }
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
    rewriteRuleSets: !empty(functionHostKey) ? [
      {
        name: 'injectFuncKey'
        properties: {
          rewriteRules: [
            {
              name: 'addFuncKeyHeader'
              ruleSequence: 100
              actionSet: {
                requestHeaderConfigurations: [
                  { headerName: 'x-functions-key', headerValue: functionHostKey }
                ]
              }
            }
          ]
        }
      }
    ] : []
    httpListeners: [
      {
        name: 'httpListener'
        properties: {
          frontendIPConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appGwFrontendIP') }
          frontendPort: { id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'port80') }
          protocol: 'Http'
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'httpToFunc'
        properties: {
          priority: 100
          ruleType: 'Basic'
          httpListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'httpListener') }
          backendAddressPool: { id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'funcBackendPool') }
          backendHttpSettings: { id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', appGwName, 'funcHttpSettings') }
          rewriteRuleSet: !empty(functionHostKey) ? { id: resourceId('Microsoft.Network/applicationGateways/rewriteRuleSets', appGwName, 'injectFuncKey') } : null
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
    funcApp
  ]
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output storageAccountName    string = storageAccount.name
output storageAccountBlobUrl string = storageAccount.properties.primaryEndpoints.blob
output acrLoginServer        string = acr.properties.loginServer
output functionAppHostname   string = funcApp.properties.defaultHostName
output appGwPublicFqdn       string = appGwPip.properties.dnsSettings.fqdn
