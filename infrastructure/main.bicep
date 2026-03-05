// ============================================================================
// Pre-OCR Preprocessing – Infrastructure (Bicep)
//
// Deploys:
//   • Storage Account (no public access, no shared keys)
//     with containers: raw, artifacts, outputs
//   • Private Endpoints for blob, queue, table, file
//   • Private DNS Zones for all four sub-resources
//   • Azure Container Registry (for the Function App Docker image)
//   • App Service Plan (Linux, B1) for the Function App
//   • Azure Function App (Docker container, Python 3.11)
//   • App Service Plan (WorkflowStandard WS1) for the Logic App
//   • Logic App Standard
//   • Application Insights
//   • VNet with integration, private-endpoint, and appgw subnets
//   • Application Gateway v2 (public entry point, injects func key)
//   • Role assignments: Blob Data Owner, Queue Data Contributor,
//     Table Data Contributor for both managed identities
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

@description('Azure AI Document Intelligence endpoint URL (optional, used by Logic App).')
param docIntelEndpoint string = 'https://PLACEHOLDER.cognitiveservices.azure.com/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30'

@description('Optional: existing VNet resource ID. Leave empty to create a new VNet.')
param existingVnetId string = ''

@description('VNet address space (used only when creating a new VNet).')
param vnetAddressPrefix string = '10.0.0.0/16'

@description('Subnet for VNet-integrated apps (Function App + Logic App).')
param integrationSubnetPrefix string = '10.0.1.0/24'

@description('Subnet for private endpoints.')
param privateEndpointSubnetPrefix string = '10.0.2.0/24'

@description('Subnet for Application Gateway.')
param appGwSubnetPrefix string = '10.0.3.0/24'

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
var logicPlanName         = '${projectName}-${env}-logic-plan'
var logicAppName          = '${projectName}-${env}-logic-${suffix}'
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
    allowSharedKeyAccess: true             // Required for Logic App connection string auth
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
  sku: { name: 'B1', tier: 'Basic' }
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

// ── Logic App Standard ──────────────────────────────────────────────────────

resource logicPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: logicPlanName
  location: location
  sku: { name: 'WS1', tier: 'WorkflowStandard' }
  kind: 'elastic'
  properties: {}
}

resource logicApp 'Microsoft.Web/sites@2023-12-01' = {
  name: logicAppName
  location: location
  kind: 'functionapp,workflowapp'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: logicPlan.id
    virtualNetworkSubnetId: integrationSubnetId
    vnetRouteAllEnabled: true
    siteConfig: {
      appSettings: [
        // Identity-based AzureWebJobsStorage (no keys!)
        { name: 'AzureWebJobsStorage__accountName',     value: storageAccount.name }
        { name: 'AzureWebJobsStorage__blobServiceUri',  value: storageAccount.properties.primaryEndpoints.blob }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: storageAccount.properties.primaryEndpoints.queue }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: storageAccount.properties.primaryEndpoints.table }
        { name: 'AzureWebJobsStorage__credential',      value: 'managedidentity' }
        { name: 'FUNCTIONS_EXTENSION_VERSION',    value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',       value: 'dotnet' }
        { name: 'WEBSITE_NODE_DEFAULT_VERSION',   value: '~18' }
        { name: 'APP_KIND',                       value: 'workflowApp' }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'STORAGE_ACCOUNT_URL',            value: storageAccount.properties.primaryEndpoints.blob }
        { name: 'PREPROCESS_FUNCTION_URL',        value: 'https://${funcApp.properties.defaultHostName}/api/preprocess' }
        { name: 'DOC_INTEL_ENDPOINT',             value: docIntelEndpoint }
        // Blob connector – managed-identity endpoint (used by connections.json)
        { name: 'AzureBlob__blobServiceUri',      value: storageAccount.properties.primaryEndpoints.blob }
        { name: 'WEBSITE_VNET_ROUTE_ALL',         value: '1' }
        { name: 'WEBSITE_DNS_SERVER',             value: '168.63.129.16' }
      ]
    }
  }
}

// ── Role Assignments ────────────────────────────────────────────────────────
// Both Function App and Logic App need:
//   • Storage Blob Data Owner        – runtime manages lease blobs
//   • Storage Queue Data Contributor – runtime uses internal queues
//   • Storage Table Data Contributor – runtime uses internal tables
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

// -- Logic App roles --
resource logicBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, logicApp.id, storageBlobDataOwner)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logicQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, logicApp.id, storageQueueDataContributor)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributor)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logicTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, logicApp.id, storageTableDataContributor)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributor)
    principalId: logicApp.identity.principalId
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

// ── Outputs ─────────────────────────────────────────────────────────────────

output storageAccountName    string = storageAccount.name
output storageAccountBlobUrl string = storageAccount.properties.primaryEndpoints.blob
output acrLoginServer        string = acr.properties.loginServer
output functionAppHostname   string = funcApp.properties.defaultHostName
output logicAppHostname      string = logicApp.properties.defaultHostName
output appGwPublicFqdn       string = appGwPip.properties.dnsSettings.fqdn
