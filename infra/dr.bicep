// Phase I: disaster recovery pilot light in a second region (westus3, Phoenix: US; the Function tool our agent needs isn't offered in South Central US).
// Resource group rg-tx-fiscal-radar-dr. Everything here is pay-per-use, so it costs ~nothing while idle:
//   Foundry account + project + the same guardrail policy + gpt-4.1-mini + text-embedding-3-small   (AI layer)
//   Function App (Flex, scale to zero) running the same code against the DR agent                    (app layer)
// The data tier (SQL, AI Search, lake) stays in the primary region: the lake is geo-redundant (GRS); SQL and the index
// are rebuilt from it (docs/DR.md). Deploy: infra/dr_up.sh. Remove: az group delete -n rg-tx-fiscal-radar-dr.

param location string = resourceGroup().location
@description('Primary resource group (for cross-RG role assignments and names)')
param primaryRg string = 'rg-tx-fiscal-radar-dev'
@description('Suffix of the primary deployment (uniqueString of the primary RG id, 6 chars)')
param primarySuffix string
param primaryAppPrincipalId string
param appClientId string
param drAgentId string = ''
param searchName string
param sqlServerFqdn string
param sqlDatabase string
param chatCapacityK int = 50
@description('Primary Log Analytics workspace resource id: the DR app reports into the same place (one view for both)')
param logWorkspaceResourceId string
@description('Operator (az login user): creates the DR agent and runs drills')
param operatorObjectId string

var tags = {
  project: 'tx-fiscal-radar'
  environment: 'dr'
  owner: 'williemaize'
  'cost-center': 'personal'
  'managed-by': 'bicep'
  repository: 'github.com/godot107/tx-fiscal-radar'
  component: 'dr'
}
var name = 'txfr-dr-${primarySuffix}'
var roles = {
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  blobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  tableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

// ---------------------------------------------------------------- AI layer
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-${name}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: 'ai-${name}'
    disableLocalAuth: true
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: 'tx-fiscal-radar-dr'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: { displayName: 'tx-fiscal-radar-dr', description: 'DR pilot light for the session analyst agent' }
}

var harmFilters = [for i in range(0, 8): {
  name: ['Hate', 'Sexual', 'Violence', 'Selfharm'][i / 2]
  source: i % 2 == 0 ? 'Prompt' : 'Completion'
  blocking: true
  enabled: true
  severityThreshold: i < 2 ? 'Low' : 'Medium'
}]

resource guardrails 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01' = {
  parent: foundry
  name: 'txfr-guardrails'
  dependsOn: [ project ]
  tags: tags
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Blocking'
    contentFilters: concat(harmFilters, [
      { name: 'Jailbreak', source: 'Prompt', blocking: true, enabled: true }
      { name: 'Indirect Attack', source: 'Prompt', blocking: true, enabled: true }
      { name: 'Protected Material Text', source: 'Completion', blocking: true, enabled: true }
      { name: 'Protected Material Code', source: 'Completion', blocking: false, enabled: true }
    ])
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'gpt-4.1-mini'
  sku: { name: 'GlobalStandard', capacity: chatCapacityK }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1-mini', version: '2025-04-14' }
    raiPolicyName: guardrails.name
  }
}

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'text-embedding-3-small'
  dependsOn: [ chat ]
  sku: { name: 'GlobalStandard', capacity: 100 }
  properties: { model: { format: 'OpenAI', name: 'text-embedding-3-small', version: '1' } }
}

// ---------------------------------------------------------------- app layer (pilot light, scale to zero)
resource st 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stdr${replace(name, '-', '')}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: { allowSharedKeyAccess: false, allowBlobPublicAccess: false, minimumTlsVersion: 'TLS1_2', supportsHttpsTrafficOnly: true }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for c in ['deployments', 'answercache']: {
  parent: blob
  name: c
}]

resource tables 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource spend 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tables
  name: 'spend'
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logWorkspaceResourceId, RetentionInDays: 30 }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-${name}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${name}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: { storage: { type: 'blobContainer', value: '${st.properties.primaryEndpoints.blob}deployments', authentication: { type: 'SystemAssignedIdentity' } } }
      scaleAndConcurrency: { maximumInstanceCount: 5, instanceMemoryMB: 2048, triggers: { http: { perInstanceConcurrency: 4 } } }
      runtime: { name: 'python', version: '3.12' }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: st.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
        { name: 'PYTHON_THREADPOOL_THREAD_COUNT', value: '4' }
        { name: 'FOUNDRY_PROJECT_ENDPOINT', value: 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}' }
        { name: 'OPENAI_ENDPOINT', value: 'https://${foundry.name}.openai.azure.com/' }
        { name: 'CHAT_DEPLOYMENT', value: chat.name }
        { name: 'EMBED_DEPLOYMENT', value: embed.name }
        { name: 'SEARCH_ENDPOINT', value: 'https://${searchName}.search.windows.net' }
        { name: 'SQL_SERVER', value: sqlServerFqdn }
        { name: 'SQL_DATABASE', value: sqlDatabase }
        { name: 'AGENT_ID', value: drAgentId }
        { name: 'APP_STORAGE_ACCOUNT', value: st.name }
        { name: 'TXFR_GUARDRAILS', value: '1' }
        { name: 'TXFR_MEMORY', value: '0' }      // memory lives in the primary project; off in DR
        { name: 'TXFR_SBS_SHARE', value: '0' }
        { name: 'TXFR_CACHE', value: '1' }
        { name: 'TXFR_USER_DAILY_USD', value: '1.0' }
        { name: 'TXFR_LOG_SALT', value: uniqueString(subscription().id, primaryRg, 'txfr-log-pseudonym', primarySuffix) }
      ]
    }
  }
}

resource appNoFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

resource appNoScmBasic 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}

resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: { requireAuthentication: true, unauthenticatedClientAction: 'RedirectToLoginPage', redirectToProvider: 'azureactivedirectory' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: { clientId: appClientId, openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0' }
        validation: { allowedAudiences: [ 'api://${appClientId}', appClientId ] }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

// ---------------------------------------------------------------- roles (DR RG)
var drAppRoles = [roles.azureAIUser, roles.cognitiveServicesUser, roles.cognitiveServicesOpenAIUser]

resource appOnFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in drAppRoles: {
  name: guid(foundry.id, app.id, r)
  scope: foundry
  properties: { principalId: app.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

resource operatorOnFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in drAppRoles: {
  name: guid(foundry.id, operatorObjectId, r)
  scope: foundry
  properties: { principalId: operatorObjectId, principalType: 'User', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

// the PRIMARY app fails over to this model + agent when its own region's model is unavailable
resource primaryOnFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in drAppRoles: {
  name: guid(foundry.id, primaryAppPrincipalId, r)
  scope: foundry
  properties: { principalId: primaryAppPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

resource projectModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, project.id, roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: { principalId: project.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser) }
}

resource accountModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, 'account-mi', roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: { principalId: foundry.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser) }
}

resource appOnStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in [roles.blobDataOwner, roles.tableDataContributor]: {
  name: guid(st.id, app.id, r)
  scope: st
  properties: { principalId: app.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

// DR app reads the primary search index (cross-RG role, module scoped to the primary RG)
module searchRole 'dr_search_role.bicep' = {
  name: 'dr-search-role'
  scope: resourceGroup(primaryRg)
  params: { searchName: searchName, principalId: app.identity.principalId }
}

output drFoundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output drOpenaiEndpoint string = 'https://${foundry.name}.openai.azure.com/'
output drAppName string = app.name
output drAppUrl string = 'https://${app.properties.defaultHostName}'
output drAppHost string = app.properties.defaultHostName
