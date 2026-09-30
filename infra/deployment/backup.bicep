targetScope = 'resourceGroup'

param location string
param prefix string
param tags object
param vnetAddressPrefixes array
param acaSubnetPrefix string
param privateEndpointSubnetPrefix string
param dockerBridgeCidr string
param platformReservedCidr string
param platformReservedDnsIp string
param schedule string
param restoreSchedule string
param restoreScheduleEnabled bool
param restoreAccessEnabled bool
param immutabilityDays int
param lifecycleDeleteAfterDays int
param excludedTables array
param sourceCosmosAccountResourceId string
param backupImage string
param scheduleEnabled bool
param storageSkuName string
param alertEmails array
param blobWriterRoleDefinitionId string
param keyWrapperRoleDefinitionId string
param keyUnwrapperRoleDefinitionId string

var suffix = uniqueString(subscription().id, resourceGroup().id)
var names = {
  vnet: '${prefix}-backup-vnet'
  nsg: '${prefix}-aca-nsg'
  acaSubnet: 'snet-aca'
  peSubnet: 'snet-private-endpoints'
  identity: '${prefix}-backup-uami'
  restoreIdentity: '${prefix}-restore-uami'
  restoreAccount: take(toLower(replace('${prefix}${suffix}restore', '-', '')), 44)
  restoreJob: '${prefix}-monthly-restore-test'
  environment: '${prefix}-aca-env'
  job: '${prefix}-daily-backup'
  workspace: '${prefix}-law'
  appInsights: '${prefix}-appi'
  storage: take(toLower(replace('${prefix}${suffix}backup', '-', '')), 24)
  vault: take(toLower('${prefix}-${suffix}-kv'), 24)
  registry: take(toLower(replace('${prefix}${suffix}acr', '-', '')), 50)
  key: 'backup-kek'
  container: 'backups'
}
var acaSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', names.vnet, names.acaSubnet)
var peSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', names.vnet, names.peSubnet)
var restoreAccountId = resourceId('Microsoft.DocumentDB/databaseAccounts', names.restoreAccount)
// Fail the deployment before creating permissions if a caller points the source parameter at the isolated target.
var validatedRestoreAccountName = toLower(sourceCosmosAccountResourceId) == toLower(restoreAccountId) ? fail('The restore-test Cosmos account cannot equal the source Cosmos account.') : names.restoreAccount
var backupImageParts = split(backupImage, '@sha256:')
var validatedBackupImage = length(backupImageParts) == 2 && length(last(backupImageParts)) == 64 ? backupImage : fail('backupImage must be pinned with a 64-character @sha256: digest.')
var restoreContributorRoleDefinitionId = '${restoreAccountId}/tableRoleDefinitions/00000000-0000-0000-0000-000000000002'
var sourceCosmosAccountName = toLower(last(split(sourceCosmosAccountResourceId, '/')))
var sourceCosmosTableEndpoint = 'https://${sourceCosmosAccountName}.table.cosmos.azure.com'
var cosmosZoneId = resourceId('Microsoft.Network/privateDnsZones', 'privatelink.table.cosmos.azure.com')
var blobZoneId = resourceId('Microsoft.Network/privateDnsZones', 'privatelink.blob.${environment().suffixes.storage}')
var vaultZoneId = resourceId('Microsoft.Network/privateDnsZones', 'privatelink.vaultcore.azure.net')
var acrZoneId = resourceId('Microsoft.Network/privateDnsZones', 'privatelink.azurecr.io')

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'backup-identity'
  params: {
    name: names.identity
    location: location
    tags: tags
  }
}

module restoreIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'restore-identity'
  params: {
    name: names.restoreIdentity
    location: location
    tags: union(tags, { purpose: 'governed-restore-validation', dormant: string(!restoreAccessEnabled) })
  }
}

module nsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'aca-nsg'
  params: {
    name: names.nsg
    location: location
    tags: tags
    securityRules: [
      {
        name: 'AllowVnetInbound'
        properties: {
          access: 'Allow'
          description: 'ACA infrastructure and internal health traffic; no application ingress is exposed.'
          destinationAddressPrefix: acaSubnetPrefix
          destinationPortRange: '*'
          direction: 'Inbound'
          priority: 100
          protocol: '*'
          sourceAddressPrefix: vnetAddressPrefixes[0]
          sourcePortRange: '*'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          access: 'Deny'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
          direction: 'Inbound'
          priority: 4095
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
      {
        name: 'AllowPrivateEndpointsHttps'
        properties: {
          access: 'Allow'
          description: 'Blob, Key Vault, ACR and Cosmos private endpoint traffic.'
          destinationAddressPrefix: privateEndpointSubnetPrefix
          destinationPortRange: '443'
          direction: 'Outbound'
          priority: 100
          protocol: 'Tcp'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'AllowAzureIdentityHttps'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: 'AzureActiveDirectory'
          destinationPortRange: '443'
          direction: 'Outbound'
          priority: 110
          protocol: 'Tcp'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'AllowAzureMonitorHttps'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: 'AzureMonitor'
          destinationPortRange: '443'
          direction: 'Outbound'
          priority: 120
          protocol: 'Tcp'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'AllowAcaControlPlaneHttps'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: 'AzureContainerApps'
          destinationPortRange: '443'
          direction: 'Outbound'
          priority: 130
          protocol: 'Tcp'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'AllowDnsToAzureResolver'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '168.63.129.16'
          destinationPortRange: '53'
          direction: 'Outbound'
          priority: 140
          protocol: '*'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'DenyInternetOutbound'
        properties: {
          access: 'Deny'
          description: 'Explicitly block arbitrary Internet egress after narrow platform/private-link allows.'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '*'
          direction: 'Outbound'
          priority: 4000
          protocol: '*'
          sourceAddressPrefix: acaSubnetPrefix
          sourcePortRange: '*'
        }
      }
      {
        name: 'DenyAllOutbound'
        properties: {
          access: 'Deny'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
          direction: 'Outbound'
          priority: 4095
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
    ]
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'backup-vnet'
  params: {
    name: names.vnet
    location: location
    tags: tags
    addressPrefixes: vnetAddressPrefixes
    subnets: [
      {
        name: names.acaSubnet
        addressPrefix: acaSubnetPrefix
        networkSecurityGroupResourceId: nsg.outputs.resourceId
        delegation: 'Microsoft.App/environments'
        privateEndpointNetworkPolicies: 'Disabled'
      }
      {
        name: names.peSubnet
        addressPrefix: privateEndpointSubnetPrefix
        privateEndpointNetworkPolicies: 'Disabled'
      }
    ]
  }
}

module cosmosDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'cosmos-dns'
  params: {
    name: 'privatelink.table.cosmos.azure.com'
    tags: tags
    virtualNetworkLinks: [
      { name: '${names.vnet}-link', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
  }
}
module blobDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'blob-dns'
  params: {
    name: 'privatelink.blob.${environment().suffixes.storage}'
    tags: tags
    virtualNetworkLinks: [
      { name: '${names.vnet}-link', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
  }
}
module vaultDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'vault-dns'
  params: {
    name: 'privatelink.vaultcore.azure.net'
    tags: tags
    virtualNetworkLinks: [
      { name: '${names.vnet}-link', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
  }
}
module acrDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'acr-dns'
  params: {
    name: 'privatelink.azurecr.io'
    tags: tags
    virtualNetworkLinks: [
      { name: '${names.vnet}-link', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
  }
}

module workspace 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'log-analytics'
  params: {
    name: names.workspace
    location: location
    tags: tags
    dataRetention: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

module appInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'application-insights'
  params: {
    name: names.appInsights
    location: location
    tags: tags
    workspaceResourceId: workspace.outputs.resourceId
    kind: 'web'
    applicationType: 'web'
    disableLocalAuth: true
    retentionInDays: 90
    roleAssignments: concat([
      {
        principalId: identity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: '3913510d-42f4-4e42-8a64-420c390055eb'
      }
    ], restoreAccessEnabled ? [
      {
        principalId: restoreIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: '3913510d-42f4-4e42-8a64-420c390055eb'
      }
    ] : [])
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.33.1' = {
  name: 'backup-storage'
  params: {
    name: names.storage
    location: location
    tags: tags
    skuName: storageSkuName
    kind: 'StorageV2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
    blobServices: {
      isVersioningEnabled: true
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: immutabilityDays
      containerDeleteRetentionPolicyEnabled: true
      containerDeleteRetentionPolicyDays: immutabilityDays
      containers: [
        {
          name: names.container
          publicAccess: 'None'
          immutableStorageWithVersioningEnabled: true
          immutabilityPolicy: {
            immutabilityPeriodSinceCreationInDays: immutabilityDays
            allowProtectedAppendWrites: false
            allowProtectedAppendWritesAll: false
          }
          roleAssignments: concat([
            {
              principalId: identity.outputs.principalId
              principalType: 'ServicePrincipal'
              roleDefinitionIdOrName: blobWriterRoleDefinitionId
            }
          ], restoreAccessEnabled ? [
            {
              principalId: restoreIdentity.outputs.principalId
              principalType: 'ServicePrincipal'
              roleDefinitionIdOrName: 'Storage Blob Data Reader'
            }
          ] : [])
        }
      ]
    }
    managementPolicyRules: [
      {
        name: 'delete-expired-backups'
        enabled: true
        type: 'Lifecycle'
        definition: {
          actions: {
            baseBlob: { delete: { daysAfterModificationGreaterThan: lifecycleDeleteAfterDays } }
            version: { delete: { daysAfterCreationGreaterThan: lifecycleDeleteAfterDays } }
          }
          filters: { blobTypes: [ 'blockBlob' ], prefixMatch: [ '${names.container}/' ] }
        }
      }
    ]
    privateEndpoints: [
      {
        name: '${names.storage}-blob-pe'
        subnetResourceId: peSubnetId
        service: 'blob'
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            { name: 'blob', privateDnsZoneResourceId: blobZoneId }
          ]
        }
      }
    ]
    diagnosticSettings: [ { workspaceResourceId: workspace.outputs.resourceId } ]
  }
  dependsOn: [ blobDns, vnet ]
}

module vault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: 'backup-key-vault'
  params: {
    name: names.vault
    location: location
    tags: tags
    sku: 'premium'
    enableRbacAuthorization: true
    enablePurgeProtection: true
    enableVaultForDeployment: false
    enableVaultForTemplateDeployment: false
    enableVaultForDiskEncryption: false
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
    // ARM key lifecycle operations require the trusted Azure services bypass while public access remains disabled.
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
    keys: [
      {
        name: names.key
        kty: 'RSA-HSM'
        keySize: 3072
        keyOps: [ 'wrapKey', 'unwrapKey' ]
        rotationPolicy: {
          attributes: { expiryTime: 'P2Y' }
          lifetimeActions: [
            { action: { type: 'rotate' }, trigger: { timeBeforeExpiry: 'P60D' } }
            { action: { type: 'notify' }, trigger: { timeBeforeExpiry: 'P30D' } }
          ]
        }
        roleAssignments: concat([
          {
            principalId: identity.outputs.principalId
            principalType: 'ServicePrincipal'
            roleDefinitionIdOrName: keyWrapperRoleDefinitionId
          }
        ], restoreAccessEnabled ? [
          {
            principalId: restoreIdentity.outputs.principalId
            principalType: 'ServicePrincipal'
            roleDefinitionIdOrName: keyUnwrapperRoleDefinitionId
          }
        ] : [])
      }
    ]
    privateEndpoints: [
      {
        name: '${names.vault}-pe'
        subnetResourceId: peSubnetId
        service: 'vault'
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            { name: 'vault', privateDnsZoneResourceId: vaultZoneId }
          ]
        }
      }
    ]
    diagnosticSettings: [ { workspaceResourceId: workspace.outputs.resourceId } ]
  }
  dependsOn: [ vaultDns, vnet ]
}

module registry 'br/public:avm/res/container-registry/registry:0.13.1' = {
  name: 'backup-registry'
  params: {
    #disable-next-line BCP334
    name: names.registry
    location: location
    tags: tags
    acrSku: 'Premium'
    acrAdminUserEnabled: false
    exportPolicyStatus: 'enabled'
    // Development-stage publishing uses GitHub-hosted runners. Keep the private endpoint for workload pulls.
    publicNetworkAccess: 'Enabled'
    networkRuleSetDefaultAction: 'Allow'
    zoneRedundancy: 'Enabled'
    roleAssignments: [
      {
        principalId: identity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
      {
        principalId: restoreIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
    ]
    privateEndpoints: [
      {
        name: '${names.registry}-pe'
        subnetResourceId: peSubnetId
        service: 'registry'
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            { name: 'registry', privateDnsZoneResourceId: acrZoneId }
          ]
        }
      }
    ]
    diagnosticSettings: [ { workspaceResourceId: workspace.outputs.resourceId } ]
  }
  dependsOn: [ acrDns, vnet ]
}

// The restore target is a dedicated Table API account in the backup security boundary. The validated
// name fails deployment if the source ID points to this target; global uniqueness is an additional guard.
// The restore UAMI receives no source-account assignment anywhere here.
resource restoreTableAccount 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  #disable-next-line BCP334
  name: validatedRestoreAccountName
}

resource restoreTableContributor 'Microsoft.DocumentDB/databaseAccounts/tableRoleAssignments@2026-03-15' = if (restoreAccessEnabled) {
  parent: restoreTableAccount
  name: guid(restoreAccountId, resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', names.restoreIdentity), restoreContributorRoleDefinitionId)
  properties: {
    principalId: restoreIdentity.outputs.principalId
    roleDefinitionId: restoreContributorRoleDefinitionId
    scope: restoreAccountId
  }
  dependsOn: [ restoreAccount ]
}

module restoreAccount 'br/public:avm/res/document-db/database-account:0.21.1' = {
  name: 'restore-test-cosmos-account'
  params: {
    #disable-next-line BCP334
    name: validatedRestoreAccountName
    location: location
    tags: union(tags, { purpose: 'isolated-restore-validation', sourceData: 'prohibited' })
    capabilitiesToAdd: [ 'EnableTable' ]
    capacityMode: 'Serverless'
    enableBurstCapacity: false
    zoneRedundant: false
    disableLocalAuthentication: true
    disableKeyBasedMetadataWriteAccess: true
    minimumTlsVersion: 'Tls12'
    networkRestrictions: {
      ipRules: []
      virtualNetworkRules: []
      networkAclBypass: 'None'
      publicNetworkAccess: 'Disabled'
    }
    privateEndpoints: [
      {
        name: '${names.restoreAccount}-table-pe'
        subnetResourceId: peSubnetId
        service: 'Table'
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            { name: 'table', privateDnsZoneResourceId: cosmosZoneId }
          ]
        }
      }
    ]
    diagnosticSettings: [ { workspaceResourceId: workspace.outputs.resourceId } ]
  }
  dependsOn: [ cosmosDns, vnet ]
}

module cosmosPrivateEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = {
  name: 'source-cosmos-private-endpoint'
  params: {
    name: '${prefix}-source-table-pe'
    location: location
    tags: tags
    subnetResourceId: peSubnetId
    manualPrivateLinkServiceConnections: [
      {
        name: 'table'
        properties: {
          privateLinkServiceId: sourceCosmosAccountResourceId
          groupIds: [ 'Table' ]
          requestMessage: 'Phase 1 isolated logical backup reader'
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'table'
      privateDnsZoneGroupConfigs: [
        { name: 'table', privateDnsZoneResourceId: cosmosZoneId }
      ]
    }
  }
  dependsOn: [ cosmosDns, vnet ]
}

module environmentModule 'br/public:avm/res/app/managed-environment:0.16.0' = {
  name: 'container-apps-environment'
  params: {
    name: names.environment
    location: location
    tags: tags
    infrastructureSubnetResourceId: acaSubnetId
    internal: true
    publicNetworkAccess: 'Disabled'
    zoneRedundant: true
    dockerBridgeCidr: dockerBridgeCidr
    platformReservedCidr: platformReservedCidr
    platformReservedDnsIP: platformReservedDnsIp
    infrastructureResourceGroupName: '${prefix}-aca-infra'
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: workspace.outputs.resourceId
    }
  }
  dependsOn: [ vnet ]
}

module job 'br/public:avm/res/app/job:0.7.2' = {
  name: 'scheduled-backup-job'
  params: {
    name: names.job
    location: location
    tags: union(tags, { ScheduleEnabled: string(scheduleEnabled) })
    environmentResourceId: environmentModule.outputs.resourceId
    triggerType: scheduleEnabled ? 'Schedule' : 'Manual'
    scheduleTriggerConfig: scheduleEnabled ? { cronExpression: schedule, parallelism: 1, replicaCompletionCount: 1 } : null
    manualTriggerConfig: scheduleEnabled ? null : { parallelism: 1, replicaCompletionCount: 1 }
    replicaRetryLimit: 1
    replicaTimeout: 7200
    workloadProfileName: 'Consumption'
    managedIdentities: { userAssignedResourceIds: [ identity.outputs.resourceId ] }
    registries: [ { server: '${names.registry}.azurecr.io', identity: identity.outputs.resourceId } ]
    containers: [
      {
        name: 'backup'
        image: validatedBackupImage
        resources: { cpu: '1.0', memory: '2Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
          { name: 'COSMOS_TABLE_ENDPOINT', value: sourceCosmosTableEndpoint }
          { name: 'BACKUP_STORAGE_ACCOUNT_URL', value: 'https://${names.storage}.blob.${environment().suffixes.storage}' }
          { name: 'BACKUP_CONTAINER_NAME', value: names.container }
          { name: 'BACKUP_KEY_ID', value: vault.outputs.keys[0].uriWithVersion }
          { name: 'EXCLUDED_TABLES_JSON', value: string(excludedTables) }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
          { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD;ClientId=${identity.outputs.clientId}' }
        ]
      }
    ]
  }
  // The versioned vault key output creates the Key Vault dependency implicitly.
  dependsOn: [ storage, registry, cosmosPrivateEndpoint ]
}

module restoreJob 'br/public:avm/res/app/job:0.7.2' = {
  name: 'monthly-restore-validation-job'
  params: {
    name: names.restoreJob
    location: location
    tags: union(tags, {
      ScheduleEnabled: string(restoreScheduleEnabled)
      AccessEnabled: string(restoreAccessEnabled)
      purpose: 'isolated-restore-validation'
    })
    environmentResourceId: environmentModule.outputs.resourceId
    triggerType: (restoreScheduleEnabled && restoreAccessEnabled) ? 'Schedule' : 'Manual'
    scheduleTriggerConfig: (restoreScheduleEnabled && restoreAccessEnabled) ? {
      cronExpression: restoreSchedule
      parallelism: 1
      replicaCompletionCount: 1
    } : null
    manualTriggerConfig: (restoreScheduleEnabled && restoreAccessEnabled) ? null : {
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaRetryLimit: 0
    replicaTimeout: 14400
    workloadProfileName: 'Consumption'
    managedIdentities: { userAssignedResourceIds: [ restoreIdentity.outputs.resourceId ] }
    registries: [ { server: '${names.registry}.azurecr.io', identity: restoreIdentity.outputs.resourceId } ]
    containers: [
      {
        name: 'restore-validation'
        image: validatedBackupImage
        command: [ 'python', '-m', 'cosmos_table_backup.cli' ]
        args: [ 'restore-test' ]
        resources: { cpu: '1.0', memory: '2Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: restoreIdentity.outputs.clientId }
          { name: 'BACKUP_STORAGE_ACCOUNT_URL', value: 'https://${names.storage}.blob.${environment().suffixes.storage}' }
          { name: 'BACKUP_CONTAINER', value: names.container }
          { name: 'KEY_VAULT_KEY_ID', value: 'https://${names.vault}${environment().suffixes.keyvaultDns}/keys/${names.key}' }
          { name: 'RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID', value: restoreAccount.outputs.resourceId }
          { name: 'RESTORE_TARGET_TABLE_ENDPOINT', value: 'https://${names.restoreAccount}.table.cosmos.azure.com' }
          { name: 'RESTORE_SOURCE_ACCOUNT_RESOURCE_ID', value: sourceCosmosAccountResourceId }
          { name: 'RESTORE_REQUIRE_ISOLATED_TARGET', value: 'true' }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
          { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD;ClientId=${restoreIdentity.outputs.clientId}' }
        ]
      }
    ]
  }
  dependsOn: [ storage, vault, registry ]
}

module monitoring '../resources/monitoring.bicep' = {
  name: 'backup-monitoring-alerts'
  params: {
    location: location
    prefix: prefix
    tags: tags
    workspaceResourceId: workspace.outputs.resourceId
    restoreScheduleEnabled: restoreScheduleEnabled && restoreAccessEnabled
    alertEmails: alertEmails
  }
}

output backupIdentityPrincipalId string = identity.outputs.principalId
output backupIdentityResourceId string = identity.outputs.resourceId
output restoreIdentityPrincipalId string = restoreIdentity.outputs.principalId
output restoreIdentityResourceId string = restoreIdentity.outputs.resourceId
output restoreTestCosmosAccountResourceId string = restoreAccount.outputs.resourceId
output backupJobResourceId string = job.outputs.resourceId
output restoreJobResourceId string = restoreJob.outputs.resourceId
output cosmosPrivateEndpointName string = cosmosPrivateEndpoint.outputs.name
output cosmosPrivateEndpointResourceId string = cosmosPrivateEndpoint.outputs.resourceId
output storageAccountName string = names.storage
output keyVaultName string = names.vault
output backupKeyVersionUri string = vault.outputs.keys[0].uriWithVersion
output registryName string = names.registry
