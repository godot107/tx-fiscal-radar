// dev environment. Deploy: infra/up.sh
using '../main.bicep'

param environmentName = 'dev'
param sparkAutoPauseMinutes = 5
param budgetAmounts = [25, 50, 100]

// Identity- and machine-specific values come from the deploying shell (never committed):
param operatorObjectId = readEnvironmentVariable('OPERATOR_OBJECT_ID')
param operatorLogin = readEnvironmentVariable('OPERATOR_LOGIN', 'operator')
param clientIps = json(readEnvironmentVariable('CLIENT_IPS', '[]'))

// Foundry → AI Search connection: every PUT returned HTTP 500 on 2026-11-10, yet the connection exists (found
// 2026-11-10 when a second PUT said "already exists"). Reuse it instead of creating it.
param existingSearchConnection = 'search'

// Phase F web app (Fiscal Note Desk). APP_CLIENT_ID comes from infra/app_auth.sh; AGENT_ID from txfr.agent create.
param deployApp = true
param appClientId = readEnvironmentVariable('APP_CLIENT_ID', '')
param agentId = readEnvironmentVariable('AGENT_ID', '')
param agentIdKnowledge = readEnvironmentVariable('AGENT_ID_KNOWLEDGE', '')
param drFoundryProjectEndpoint = readEnvironmentVariable('DR_FOUNDRY_PROJECT_ENDPOINT', '')
param drAgentId = readEnvironmentVariable('DR_AGENT_ID', '')

// Model capacity (K tokens/min). 50 normally; the Phase G load test raised it to 300 temporarily (CHAT_CAPACITY_K=300).
param chatCapacityK = int(readEnvironmentVariable('CHAT_CAPACITY_K', '50'))
