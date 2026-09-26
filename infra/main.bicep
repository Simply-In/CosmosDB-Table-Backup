targetScope = 'subscription'

@description('Target deployment region.')
param location string = 'polandcentral'

@description('Resource group for the isolated backup security boundary.')
param resourceGroupName string

@description('Short lowercase naming prefix. Storage/ACR/Key Vault names are derived from it.')
@minLength(3)
@maxLength(12)
param prefix string

@description('Resource tags applied to all supported resources.')
param tags object

@description('Address space of the isolated backup VNet.')
param vnetAddressPrefixes array
param acaSubnetPrefix string
param privateEndpointSubnetPrefix string
param dockerBridgeCidr string
param platformReservedCidr string
param platformReservedDnsIp string

@description('Daily UTC NCRONTAB schedule for the backup job.')
param schedule string
@description('Immutable retention period. Phase 1 requires at least seven days.')
@minValue(7)
param immutabilityDays int = 7
@description('Lifecycle deletion eligibility. Must exceed immutabilityDays.')
@minValue(8)
param lifecycleDeleteAfterDays int = 14
@description('Exact, case-sensitive table names excluded by the application.')
param excludedTables array = [
  'cards'
]

@description('Full resource ID of the source Cosmos DB for Table account.')
param sourceCosmosAccountResourceId string
@description('Immutable image reference. Use an ACR digest for production; bootstrap placeholder is disabled by default.')
param backupImage string
@description('Set true only after the image is pushed and smoke-test prerequisites are complete.')
param scheduleEnabled bool = false
@description('Monthly UTC NCRONTAB schedule for isolated restore validation.')
param restoreSchedule string = '0 4 1 * *'
@description('Enables the monthly restore-validation schedule. False keeps the job manually triggered.')
param restoreScheduleEnabled bool = false
@description('Activates restore-only data-plane grants. False leaves the restore UAMI dormant.')
param restoreAccessEnabled bool = false
@description('Storage redundancy SKU.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
  'Standard_GZRS'
])
param storageSkuName string = 'Standard_ZRS'
@description('Optional alert email receivers. Empty creates alerts without email actions.')
param alertEmails array = []

var blobWriterRoleId = guid(subscription().id, 'CosmosTableBackupBlobAppendWriter')
var keyWrapperRoleId = guid(subscription().id, 'CosmosTableBackupKeyWrapper')
var keyUnwrapperRoleId = guid(subscription().id, 'CosmosTableRestoreKeyUnwrapper')

resource resourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Native role definitions are required: Azure built-ins include Blob delete/read and Key Vault unwrap,
// which violate the one-way backup runtime boundary.
resource blobWriterRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: blobWriterRoleId
  properties: {
    roleName: '${prefix} Blob create-only backup writer'
    description: 'Writes/adds blobs and reads container metadata; cannot read blob data or delete.'
    type: 'CustomRole'
    assignableScopes: [
      subscription().id
    ]
    permissions: [
      {
        actions: [
          'Microsoft.Storage/storageAccounts/read'
          'Microsoft.Storage/storageAccounts/blobServices/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/read'
        ]
        notActions: []
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
        ]
        notDataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'
        ]
      }
    ]
  }
}

resource keyWrapperRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: keyWrapperRoleId
  properties: {
    roleName: '${prefix} Key metadata and wrap only'
    description: 'Reads public key metadata and wraps DEKs; cannot unwrap or administer keys.'
    type: 'CustomRole'
    assignableScopes: [
      subscription().id
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.KeyVault/vaults/keys/read'
          'Microsoft.KeyVault/vaults/keys/wrap/action'
        ]
        notDataActions: [
          'Microsoft.KeyVault/vaults/keys/unwrap/action'
        ]
      }
    ]
  }
}

resource keyUnwrapperRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: keyUnwrapperRoleId
  properties: {
    roleName: '${prefix} Key metadata and unwrap only'
    description: 'Restore validation reads key metadata and unwraps DEKs; cannot wrap or administer keys.'
    type: 'CustomRole'
    assignableScopes: [
      subscription().id
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.KeyVault/vaults/keys/read'
          'Microsoft.KeyVault/vaults/keys/unwrap/action'
        ]
        notDataActions: [
          'Microsoft.KeyVault/vaults/keys/wrap/action'
        ]
      }
    ]
  }
}

module backup './deployment/backup.bicep' = {
  name: 'backup-domain'
  scope: resourceGroup
  params: {
    location: location
    prefix: prefix
    tags: tags
    vnetAddressPrefixes: vnetAddressPrefixes
    acaSubnetPrefix: acaSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    dockerBridgeCidr: dockerBridgeCidr
    platformReservedCidr: platformReservedCidr
    platformReservedDnsIp: platformReservedDnsIp
    schedule: schedule
    restoreSchedule: restoreSchedule
    restoreScheduleEnabled: restoreScheduleEnabled
    restoreAccessEnabled: restoreAccessEnabled
    immutabilityDays: immutabilityDays
    lifecycleDeleteAfterDays: lifecycleDeleteAfterDays
    excludedTables: excludedTables
    sourceCosmosAccountResourceId: sourceCosmosAccountResourceId
    backupImage: backupImage
    scheduleEnabled: scheduleEnabled
    storageSkuName: storageSkuName
    alertEmails: alertEmails
    blobWriterRoleDefinitionId: blobWriterRole.id
    keyWrapperRoleDefinitionId: keyWrapperRole.id
    keyUnwrapperRoleDefinitionId: keyUnwrapperRole.id
  }
}

output backupIdentityPrincipalId string = backup.outputs.backupIdentityPrincipalId
output backupIdentityResourceId string = backup.outputs.backupIdentityResourceId
output restoreIdentityPrincipalId string = backup.outputs.restoreIdentityPrincipalId
output restoreIdentityResourceId string = backup.outputs.restoreIdentityResourceId
output restoreTestCosmosAccountResourceId string = backup.outputs.restoreTestCosmosAccountResourceId
output restoreJobResourceId string = backup.outputs.restoreJobResourceId
output backupKeyVersionUri string = backup.outputs.backupKeyVersionUri
output cosmosPrivateEndpointResourceId string = backup.outputs.cosmosPrivateEndpointResourceId
output sourceIntegrationParameters object = {
  backupIdentityPrincipalId: backup.outputs.backupIdentityPrincipalId
  cosmosPrivateEndpointName: backup.outputs.cosmosPrivateEndpointName
  cosmosPrivateEndpointResourceId: backup.outputs.cosmosPrivateEndpointResourceId
}
