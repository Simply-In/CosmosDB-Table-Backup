using '../main.bicep'

// Deploy with: az deployment sub create --subscription <backup-subscription-id> \
//   --location polandcentral --template-file infra/main.bicep \
//   --parameters infra/parameters/nonprod.bicepparam backupImage=<acr>.azurecr.io/cosmos-table-backup@sha256:<digest>
param location = 'polandcentral'
param resourceGroupName = 'rg-cosmos-table-backup-nonprod-plc'
param prefix = 'ctbnpplc'
param tags = {
  environment: 'nonprod'
  workload: 'cosmos-table-backup'
  dataClassification: 'confidential'
  managedBy: 'bicep'
  phase: '1-and-2'
}

param vnetAddressPrefixes = [ '10.84.0.0/16' ]
param acaSubnetPrefix = '10.84.0.0/23'
param privateEndpointSubnetPrefix = '10.84.2.0/24'
param dockerBridgeCidr = '172.20.0.0/28'
param platformReservedCidr = '172.20.1.0/24'
param platformReservedDnsIp = '172.20.1.10'

param schedule = '0 2 * * *'
param scheduleEnabled = false
// Phase 2 is fail-closed: both values must be explicitly enabled for scheduled restore validation and RBAC.
param restoreSchedule = '0 4 1 * *'
param restoreScheduleEnabled = false
param restoreAccessEnabled = false
param immutabilityDays = 7
param lifecycleDeleteAfterDays = 14
param excludedTables = [ 'cards' ]
param storageSkuName = 'Standard_ZRS'
param sourceCosmosAccountResourceId = '/subscriptions/<source-subscription-id>/resourceGroups/rg-sintoken-backend-dev-plc/providers/Microsoft.DocumentDB/databaseAccounts/cdb-sintoken-backend-dev-plc'

// Safe bootstrap value while scheduleEnabled=false. Replace with the immutable digest produced by CI.
param backupImage = 'example.invalid/cosmos-table-backup@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param alertEmails = []
