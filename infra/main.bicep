// tx-fiscal-radar: Azure data platform for the Texas legislative fiscal-impact pipeline.
// Everything is Entra ID only: no storage keys, no SQL passwords, no secrets in pipelines (managed identities).
//   ADLS Gen2 (bronze/silver/gold) · Key Vault (RBAC) · Data Factory (system MI) · Synapse (serverless SQL +
//   auto-pausing Spark pool) · Azure SQL free offer (serving star schema) · Log Analytics · budgets · failure alert
// Deploy: infra/up.sh dev

@description('Environment name (dev only is deployed for this project)')
param environmentName string = 'dev'
param location string = resourceGroup().location

@description('Entra object ID of the operator (gets data-plane roles and is SQL/Synapse Entra admin)')
param operatorObjectId string
param operatorLogin string = 'operator'
@description('Public IPs allowed through SQL and Synapse firewalls (the operator machine)')
param clientIps array = []

param alertEmail string = 'azure@willieman.com'
param budgetAmounts array = [25, 50, 100]
param budgetStartDate string = '2026-11-01'

param sparkNodeCount int = 3          // minimum for a Synapse Spark pool
param sparkAutoPauseMinutes int = 5   // no idle compute
param logDailyCapGb string = '0.1'

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'tx-fiscal-radar'
  environment: environmentName
  owner: 'williemaize'
  'cost-center': 'personal'
  'managed-by': 'bicep'
  repository: 'github.com/godot107/tx-fiscal-radar'
}

// Built-in role definition IDs
var roles = {
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  kvSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  kvSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

// ---------------------------------------------------------------- storage: ADLS Gen2 lake
resource lake 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'sttxfr${suffix}'
  location: location
  tags: union(tags, { component: 'lake' })
  kind: 'StorageV2'
  // Phase I (DR): geo-redundant. The lake is the recovery source for everything downstream (the SQL warehouse is
  // rebuilt from gold by pl_load_sql; the free-offer database only allows local backups). Async copy to the paired
  // region (eastus2); customer-managed failover is supported for Data Lake (HNS) accounts. docs/DR.md.
  sku: { name: 'Standard_GRS' }
  properties: {
    isHnsEnabled: true               // hierarchical namespace = ADLS Gen2 (real directories, ACLs)
    allowSharedKeyAccess: false      // Entra ID (RBAC) only
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: lake
  name: 'default'
}

resource zones 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for z in ['bronze', 'silver', 'gold', 'synapse', 'backup']: {   // backup: AI extraction cache (tools/dr_backup.py)
  parent: blobs
  name: z
  properties: { publicAccess: 'None' }
}]

// ---------------------------------------------------------------- Key Vault (RBAC mode)
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'secrets' })
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true    // access by role assignment, not access policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------- monitoring
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'monitoring' })
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json(logDailyCapGb) }
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-txfr-${environmentName}'
  location: 'global'
  tags: union(tags, { component: 'monitoring' })
  properties: {
    groupShortName: 'txfr'
    enabled: true
    emailReceivers: [ { name: 'operator', emailAddress: alertEmail, useCommonAlertSchema: true } ]
  }
}

// ---------------------------------------------------------------- Data Factory
resource adf 'Microsoft.DataFactory/factories@2018-06-01' = {
  name: 'adf-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'ingest' })
  identity: { type: 'SystemAssigned' }
  properties: {
    publicNetworkAccess: 'Enabled'
  }
}

resource adfDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: adf
  properties: {
    workspaceId: logs.id
    logAnalyticsDestinationType: 'Dedicated'   // resource-specific tables: ADFPipelineRun, ADFActivityRun, ...
    logs: [
      { category: 'PipelineRuns', enabled: true }
      { category: 'ActivityRuns', enabled: true }
      { category: 'TriggerRuns', enabled: true }
    ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

// Email on any failed pipeline run (checked every 5 minutes)
resource adfFailed 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'adf-pipeline-failed'
  location: 'global'
  tags: union(tags, { component: 'monitoring' })
  properties: {
    severity: 2
    enabled: true
    scopes: [ adf.id ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [ {
        name: 'failed-runs'
        criterionType: 'StaticThresholdCriterion'
        metricName: 'PipelineFailedRuns'
        metricNamespace: 'Microsoft.DataFactory/factories'
        operator: 'GreaterThan'
        threshold: 0
        timeAggregation: 'Total'
      } ]
    }
    actions: [ { actionGroupId: actionGroup.id } ]
  }
}

// ---------------------------------------------------------------- Synapse (serverless SQL + Spark)
resource synapse 'Microsoft.Synapse/workspaces@2021-06-01' = {
  name: 'syn-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'transform' })
  identity: { type: 'SystemAssigned' }
  properties: {
    defaultDataLakeStorage: {
      accountUrl: lake.properties.primaryEndpoints.dfs
      filesystem: 'synapse'
      resourceId: lake.id
    }
    azureADOnlyAuthentication: true
    publicNetworkAccess: 'Enabled'
    managedResourceGroupName: 'rg-txfr-synapse-managed-${environmentName}'
  }
  dependsOn: [ zones ]
}

resource synapseAdmin 'Microsoft.Synapse/workspaces/administrators@2021-06-01' = {
  parent: synapse
  name: 'activeDirectory'
  properties: {
    administratorType: 'ActiveDirectory'
    login: operatorLogin
    sid: operatorObjectId
    tenantId: subscription().tenantId
  }
}

resource synapseAzure 'Microsoft.Synapse/workspaces/firewallRules@2021-06-01' = {
  parent: synapse
  name: 'AllowAllWindowsAzureIps'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource synapseClients 'Microsoft.Synapse/workspaces/firewallRules@2021-06-01' = [for (ip, i) in clientIps: {
  parent: synapse
  name: 'client-${i}'
  properties: { startIpAddress: ip, endIpAddress: ip }
}]

resource spark 'Microsoft.Synapse/workspaces/bigDataPools@2021-06-01' = {
  parent: synapse
  name: 'sparksmall'
  location: location
  tags: union(tags, { component: 'transform' })
  properties: {
    sparkVersion: '3.4'
    nodeSize: 'Small'                 // 4 vCores / 32 GB per node
    nodeSizeFamily: 'MemoryOptimized'
    nodeCount: sparkNodeCount
    autoScale: { enabled: false }
    autoPause: { enabled: true, delayInMinutes: sparkAutoPauseMinutes }
    dynamicExecutorAllocation: { enabled: false }
    sessionLevelPackagesEnabled: true
  }
}

resource synapseDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: synapse
  properties: {
    workspaceId: logs.id
    logs: [
      { category: 'BuiltinSqlReqsEnded', enabled: true }   // serverless SQL: data processed per query
      { category: 'IntegrationPipelineRuns', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------- Azure SQL (serving layer, free offer)
resource sql 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'sql' })
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true      // no SQL passwords at all
      login: operatorLogin
      sid: operatorObjectId
      tenantId: subscription().tenantId
      principalType: 'User'
    }
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sql
  name: 'txfr'
  location: location
  tags: union(tags, { component: 'sql' })
  sku: { name: 'GP_S_Gen5_2', tier: 'GeneralPurpose', family: 'Gen5', capacity: 2 }
  properties: {
    useFreeLimit: true                         // free offer: 100k vCore-seconds + 32 GB per month
    freeLimitExhaustionBehavior: 'AutoPause'   // pause instead of billing when the free amount runs out
    autoPauseDelay: 60
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}

// Blue team (detect): SQL auditing → Log Analytics (SQLSecurityAuditEvents): logins and every batch, incl. the
// app identity's queries. Volume is small (nightly load + agent SELECTs); the workspace's daily cap still applies.
resource sqlAudit 'Microsoft.Sql/servers/auditingSettings@2023-08-01-preview' = {
  parent: sql
  name: 'default'
  properties: {
    state: 'Enabled'
    isAzureMonitorTargetEnabled: true
  }
}

resource sqlMaster 'Microsoft.Sql/servers/databases@2023-08-01-preview' existing = {
  parent: sql
  name: 'master'
}

resource sqlAuditDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'audit-to-log-analytics'
  scope: sqlMaster
  properties: {
    workspaceId: logs.id
    logs: [ { category: 'SQLSecurityAuditEvents', enabled: true } ]
  }
}

resource sqlAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sql
  name: 'AllowAllWindowsAzureIps'   // ADF, Synapse and Power BI connect from Azure-hosted IPs
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource sqlClients 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = [for (ip, i) in clientIps: {
  parent: sql
  name: 'client-${i}'
  properties: { startIpAddress: ip, endIpAddress: ip }
}]

// ---------------------------------------------------------------- AI: Foundry (gpt-4.1-mini) + AI Search (free tier)
// Phase 5b: per-agency cost extraction + mandate classification from fiscal-note text.
// Phase 6b: a Foundry agent that answers questions about the session, grounded in the AI Search index.
// eastus2: Foundry Agent Service region (centralus is not one); data stays in the US.
param aiLocation string = 'eastus2'
@description('AI Search region (free-tier capacity varies by region; eastus2 had none on 2026-11-10)')
param searchLocation string = 'centralus'
param chatModel string = 'gpt-4.1-mini'
param chatModelVersion string = '2025-04-14'
@description('Model deployment capacity in thousands of tokens per minute')
param chatCapacityK int = 50

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-txfr-${suffix}'
  location: aiLocation
  tags: union(tags, { component: 'ai' })
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: 'ai-txfr-${suffix}'
    disableLocalAuth: true          // Entra ID only, no API keys
    allowProjectManagement: true    // Foundry projects (agents, evaluations)
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: 'tx-fiscal-radar'
  location: aiLocation
  tags: union(tags, { component: 'ai' })
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'tx-fiscal-radar'
    description: 'Fiscal-note extraction (phase 5b) and the session analyst agent (phase 6b)'
  }
}

// Phase B guardrails: content filters (Hate at Low since B2, others Medium) on prompts and completions, Prompt Shields for jailbreaks (user
// prompts) and indirect attacks (instructions hidden in documents/tool output), protected-material detection.
var harmFilters = [for i in range(0, 8): {
  name: ['Hate', 'Sexual', 'Violence', 'Selfharm'][i / 2]
  source: i % 2 == 0 ? 'Prompt' : 'Completion'
  blocking: true
  enabled: true
  severityThreshold: i < 2 ? 'Low' : 'Medium'   // Hate at Low (stricter): public audience, Phase B2
}]

resource guardrails 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01' = {
  parent: foundry
  name: 'txfr-guardrails'
  dependsOn: [ foundryProject ]   // the account allows one operation at a time
  tags: union(tags, { component: 'ai' })
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
  name: chatModel
  sku: { name: 'GlobalStandard', capacity: chatCapacityK }
  properties: {
    model: { format: 'OpenAI', name: chatModel, version: chatModelVersion }
    raiPolicyName: guardrails.name
  }
}

// Phase C/D: embeddings for hybrid search (256 dimensions requested at call time) and agent memory
param embedModel string = 'text-embedding-3-small'
@description('Embedding deployment capacity in thousands of tokens per minute (GlobalStandard quota: 1,000)')
param embedCapacityK int = 350

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: embedModel
  dependsOn: [ chat ]
  sku: { name: 'GlobalStandard', capacity: embedCapacityK }
  properties: {
    model: { format: 'OpenAI', name: embedModel, version: '1' }
  }
}

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: 'srch-txfr-${suffix}'
  location: searchLocation
  tags: union(tags, { component: 'ai' })
  sku: { name: 'free' }             // 50 MB, 3 indexes: enough for enacted-bill notes + fact cards
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true          // Entra ID (RBAC) only
  }
}

// Foundry → AI Search connection at account level, shared to all projects (the agent's search tool uses it;
// auth = Entra ID: the project's managed identity has Search Index Data Reader)
@description('Create the Foundry → AI Search connection. Off: on 2026-11-10 every connection PUT on this account returned HTTP 500 (account-rp, eastus2); the agent then calls AI Search through a client-side tool. See docs/RUNBOOK.md.')
param deploySearchConnection bool = false
@description('Name of a search connection that already exists on the account (created out of band: the PUT returned HTTP 500 but the connection was created). Used when deploySearchConnection is false.')
param existingSearchConnection string = ''

resource searchConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = if (deploySearchConnection) {
  parent: foundry
  name: 'search'
  dependsOn: [ embed ]   // one operation at a time on the account
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${search.name}.search.windows.net'
    authType: 'AAD'
    isSharedToAll: true
    metadata: { ApiType: 'Azure', ResourceId: search.id, location: searchLocation }
  }
}

resource foundryDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: foundry
  properties: {
    workspaceId: logs.id
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

// ---------------------------------------------------------------- role assignments (least privilege)
resource adfLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lake.id, adf.id, roles.blobDataContributor)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: adf.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource synapseLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lake.id, synapse.id, roles.blobDataContributor)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: synapse.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lake.id, operatorObjectId, roles.blobDataContributor)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource adfKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, adf.id, roles.kvSecretsUser)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsUser)
    principalId: adf.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, operatorObjectId, roles.kvSecretsOfficer)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsOfficer)
    principalId: operatorObjectId
    principalType: 'User'
  }
}


// AI: operator runs extraction + builds the index + creates the agent; the project identity queries the index

resource opOpenAI 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, operatorObjectId, roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opAzureAI 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, operatorObjectId, roles.azureAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, operatorObjectId, roles.searchIndexDataContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opSearchSvc 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, operatorObjectId, roles.searchServiceContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource projectSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, foundryProject.id, roles.searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    principalId: foundryProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource projectSearchSvc 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, foundryProject.id, roles.searchServiceContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: foundryProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Phase D: the memory store calls the chat + embedding deployments; the account identity needs the same role
resource accountModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, 'account-mi', roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: foundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Phase D: the memory store runs as the project's identity and calls the chat + embedding deployments
resource projectModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, foundryProject.id, roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: foundryProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- Phase F: Fiscal Note Desk web app
// One Function App (Flex Consumption, Python 3.12) serves the page and /api/ask. Entra sign-in (App Service
// Authentication) in front of every route; the app reaches Foundry, Content Safety, AI Search and SQL with its
// managed identity. Static Web Apps was the plan, but its Free tier can't link a Functions backend and its
// managed functions have no managed identity (docs/ARCHITECTURE.md).
@description('Deploy the web app (needs APP_CLIENT_ID from infra/app_auth.sh)')
param deployApp bool = false
param appClientId string = ''
@description('Scale-out cap: bounds cost and protects the model quota (each instance runs up to 4 questions at once)')
param appMaxInstances int = 5
param agentId string = ''
param agentIdKnowledge string = ''
@description('Nightly Power BI dataset refresh by the app identity (enable after the tenant setting + workspace access, RUNBOOK)')
param pbiRefresh bool = false
@description('Phase I: DR region project endpoint and agent id for automatic model failover (empty = no failover)')
param drFoundryProjectEndpoint string = ''
param drAgentId string = ''
@description('A/B: share of web questions answered side by side by both agent variants (0 = only when the user asks)')
param sideBySideShare string = '0.1'

resource appStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (deployApp) {
  name: 'stfntxfr${suffix}'
  location: location
  tags: union(tags, { component: 'app' })
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false     // the Functions host uses the managed identity
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource appBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (deployApp) {
  parent: appStorage
  name: 'default'
}

resource appDeployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployApp) {
  parent: appBlob
  name: 'deployments'
}

// FinOps / abuse protection: answer cache (blob) and the shared per-user daily spend counter (table)
resource appCacheContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployApp) {
  parent: appBlob
  name: 'answercache'
}

// Privacy/retention: cached answers are deleted after 2 days (they're only valid until the next nightly load anyway)
resource appLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = if (deployApp) {
  parent: appStorage
  name: 'default'
  properties: {
    policy: {
      rules: [ {
        name: 'expire-answer-cache'
        enabled: true
        type: 'Lifecycle'
        definition: {
          filters: { blobTypes: [ 'blockBlob' ], prefixMatch: [ 'answercache/' ] }
          actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 2 } } }
        }
      } ]
    }
  }
}

resource appTables 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = if (deployApp) {
  parent: appStorage
  name: 'default'
}

resource appSpendTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = if (deployApp) {
  parent: appTables
  name: 'spend'
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (deployApp) {
  name: 'appi-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'app' })
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    RetentionInDays: 30
  }
}

resource appPlan 'Microsoft.Web/serverfarms@2024-04-01' = if (deployApp) {
  name: 'asp-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'app' })
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = if (deployApp) {
  name: 'func-txfr-${suffix}'
  location: location
  tags: union(tags, { component: 'app' })
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: appPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${appStorage!.properties.primaryEndpoints.blob}deployments'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: appMaxInstances
        instanceMemoryMB: 2048
        triggers: { http: { perInstanceConcurrency: 4 } }
      }
      runtime: { name: 'python', version: '3.12' }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: appStorage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights!.properties.ConnectionString }
        { name: 'PYTHON_THREADPOOL_THREAD_COUNT', value: '4' }
        { name: 'FOUNDRY_PROJECT_ENDPOINT', value: 'https://${foundry.name}.services.ai.azure.com/api/projects/${foundryProject.name}' }
        { name: 'OPENAI_ENDPOINT', value: 'https://${foundry.name}.openai.azure.com/' }
        { name: 'CHAT_DEPLOYMENT', value: chat.name }
        { name: 'EMBED_DEPLOYMENT', value: embed.name }
        { name: 'SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
        { name: 'SQL_SERVER', value: sql.properties.fullyQualifiedDomainName }
        { name: 'SQL_DATABASE', value: sqlDb.name }
        { name: 'AGENT_ID', value: agentId }
        { name: 'AGENT_ID_KNOWLEDGE', value: agentIdKnowledge }
        { name: 'DR_FOUNDRY_PROJECT_ENDPOINT', value: drFoundryProjectEndpoint }
        { name: 'DR_AGENT_ID', value: drAgentId }
        { name: 'TXFR_SBS_SHARE', value: sideBySideShare }
        { name: 'TXFR_USER_DAILY_USD', value: '1.0' }
        { name: 'APP_STORAGE_ACCOUNT', value: appStorage.name }
        { name: 'TXFR_CACHE', value: '1' }
        { name: 'TXFR_PBI_REFRESH', value: pbiRefresh ? '1' : '0' }   // nightly Power BI reload (needs tenant setting, RUNBOOK)
        { name: 'TXFR_LOG_SALT', value: uniqueString(resourceGroup().id, 'txfr-log-pseudonym', suffix) }   // production: Key Vault reference
        { name: 'TXFR_GUARDRAILS', value: '1' }
        { name: 'TXFR_MEMORY', value: '1' }
      ]
    }
  }
}

// No FTP or basic-auth (publishing password) deployments: zip deploy authenticates with Entra
resource appNoFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = if (deployApp) {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

resource appNoScmBasic 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = if (deployApp) {
  parent: app
  name: 'scm'
  properties: { allow: false }
}

resource appAuth 'Microsoft.Web/sites/config@2024-04-01' = if (deployApp) {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: appClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [ 'api://${appClientId}', appClientId ]
        }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

var appRoles = deployApp ? [
  { scope: 'storage', role: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' }   // Storage Blob Data Owner (host, deployments, cache)
  { scope: 'storage', role: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3' }   // Storage Table Data Contributor (spend counter)
  { scope: 'foundry', role: roles.azureAIUser }                         // agents, memory store
  { scope: 'foundry', role: 'a97b65f3-24c7-4388-baec-2e87135dc908' }    // Cognitive Services User (Content Safety)
  { scope: 'foundry', role: roles.cognitiveServicesOpenAIUser }         // embeddings for hybrid search
  { scope: 'search', role: roles.searchIndexDataReader }
] : []

resource appRoleStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in filter(appRoles, x => x.scope == 'storage'): {
  name: guid(appStorage.id, 'func-txfr', r.role)
  scope: appStorage
  properties: {
    principalId: app!.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.role)
  }
}]

resource appRoleFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in filter(appRoles, x => x.scope == 'foundry'): {
  name: guid(foundry.id, 'func-txfr', r.role)
  scope: foundry
  properties: {
    principalId: app!.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.role)
  }
}]

resource appRoleSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in filter(appRoles, x => x.scope == 'search'): {
  name: guid(search.id, 'func-txfr', r.role)
  scope: search
  properties: {
    principalId: app!.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.role)
  }
}]

// Blue team (detect): guardrail events from the app (GUARD_EVENT lines in AppTraces). Alert when someone appears to
// be probing: 5+ policy blocks, removed injections or content-filter hits in 15 minutes (any users). docs/SECURITY.md.
resource guardAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (deployApp) {
  name: 'txfr-guardrail-spike'
  location: location
  tags: union(tags, { component: 'app' })
  properties: {
    displayName: 'Fiscal Note Desk: guardrail spike'
    description: 'Policy blocks, removed prompt injections or content-filter hits >= 5 in 15 minutes'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [ logs.id ]
    criteria: {
      allOf: [ {
        query: 'AppTraces | where Message startswith "GUARD_EVENT" | extend e = parse_json(substring(Message, 12)) | where tostring(e.kind) in ("policy_block", "injection_removed", "content_filter")'
        timeAggregation: 'Count'
        operator: 'GreaterThanOrEqual'
        threshold: 5
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      } ]
    }
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

// Cost circuit breaker: Azure budgets only email; this stops the app automatically when the metered spend of the last
// hour passes costBreakerUsdPerHour. Alert → action group → Logic App (managed identity, Website Contributor on the app
// only) → ARM "stop site". Restart with tools/killswitch.sh start after looking at why. docs/SECURITY.md.
@description('Stop the web app automatically when metered spend in the last hour exceeds this (USD)')
param costBreakerUsdPerHour int = 2

resource breaker 'Microsoft.Logic/workflows@2019-05-01' = if (deployApp) {
  name: 'logic-txfr-breaker-${suffix}'
  location: location
  tags: union(tags, { component: 'app' })
  identity: { type: 'SystemAssigned' }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      triggers: { manual: { type: 'Request', kind: 'Http', inputs: { schema: {} } } }
      actions: {
        stop_app: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: '${environment().resourceManager}${app!.id}/stop?api-version=2024-04-01'
            authentication: { type: 'ManagedServiceIdentity', audience: environment().resourceManager }
          }
        }
      }
    }
  }
}

resource breakerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApp) {
  name: guid(app!.id, 'breaker', 'de139f84-1756-47ae-9be6-808fbbe84772')
  scope: app
  properties: {
    principalId: breaker!.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'de139f84-1756-47ae-9be6-808fbbe84772') // Website Contributor
  }
}

resource breakerGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (deployApp) {
  name: 'ag-txfr-breaker'
  location: 'global'
  tags: union(tags, { component: 'app' })
  properties: {
    groupShortName: 'txfrbreak'
    enabled: true
    emailReceivers: [ { name: 'owner', emailAddress: alertEmail, useCommonAlertSchema: true } ]
    logicAppReceivers: [ {
      name: 'stop-app'
      resourceId: breaker!.id
      callbackUrl: listCallbackUrl('${breaker!.id}/triggers/manual', '2019-05-01').value
      useCommonAlertSchema: true
    } ]
  }
}

resource costAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (deployApp) {
  name: 'txfr-cost-breaker'
  location: location
  tags: union(tags, { component: 'app' })
  properties: {
    displayName: 'Fiscal Note Desk: spend over limit, app stopped'
    description: 'Metered spend (COST_EVENT) in the last hour above costBreakerUsdPerHour → stop the Function App'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT1H'
    scopes: [ logs.id ]
    criteria: {
      allOf: [ {
        query: 'AppTraces | where Message startswith "COST_EVENT" | extend e = parse_json(substring(Message, 11)) | summarize usd = sum(todouble(e.usd_total))'
        timeAggregation: 'Total'
        metricMeasureColumn: 'usd'
        operator: 'GreaterThan'
        threshold: costBreakerUsdPerHour
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      } ]
    }
    actions: { actionGroups: [ breakerGroup.id ] }
  }
}

// ---------------------------------------------------------------- budgets (RG scope, email at 100% actual + forecast)
resource budgets 'Microsoft.Consumption/budgets@2023-11-01' = [for amt in budgetAmounts: {
  name: 'tx-fiscal-radar-${amt}'
  properties: {
    category: 'Cost'
    amount: amt
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual100: { enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 100, thresholdType: 'Actual', contactEmails: [ alertEmail ] }
      forecast100: { enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 100, thresholdType: 'Forecasted', contactEmails: [ alertEmail ] }
    }
  }
}]

output lakeAccount string = lake.name
output lakeDfsEndpoint string = lake.properties.primaryEndpoints.dfs
output keyVaultUri string = kv.properties.vaultUri
output dataFactory string = adf.name
output dataFactoryId string = adf.id
output dataFactoryPrincipalId string = adf.identity.principalId
output synapseWorkspace string = synapse.name
output synapseResourceId string = synapse.id
output synapsePrincipalId string = synapse.identity.principalId
output synapseServerlessSql string = '${synapse.name}-ondemand.sql.azuresynapse.net'
output synapseDev string = synapse.properties.connectivityEndpoints.dev
output sparkPool string = spark.name
output sqlServer string = sql.properties.fullyQualifiedDomainName
output sqlDatabase string = sqlDb.name
output logWorkspaceId string = logs.properties.customerId
output openaiEndpoint string = 'https://${foundry.name}.openai.azure.com/'
output foundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${foundryProject.name}'
output appName string = deployApp ? app.name : ''
output appUrl string = deployApp ? 'https://${app!.properties.defaultHostName}' : ''
output appPrincipalId string = deployApp ? app!.identity.principalId : ''
output chatDeployment string = chat.name
output embedDeployment string = embed.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchConnection string = deploySearchConnection ? searchConnection.name : existingSearchConnection
