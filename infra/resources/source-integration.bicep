targetScope = 'resourceGroup'

param sourceCosmosAccountResourceId string
param sourceAccountName string
param backupIdentityPrincipalId string
param cosmosPrivateEndpointName string
param cosmosPrivateEndpointResourceId string

var tableDataReaderRoleDefinitionId = '${sourceCosmosAccountResourceId}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'

resource sourceAccount 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: sourceAccountName
}

// No AVM child module currently covers an existing Cosmos account's connection approval.
resource approvePrivateEndpoint 'Microsoft.DocumentDB/databaseAccounts/privateEndpointConnections@2025-04-15' = {
  parent: sourceAccount
  name: cosmosPrivateEndpointName
  properties: {
    privateLinkServiceConnectionState: {
      status: 'Approved'
      description: 'Approved for isolated Phase 1 logical backup service.'
    }
  }
}

// Cosmos Table native data-plane RBAC uses this ARM resource name for Table API accounts.
resource tableReaderAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: sourceAccount
  name: guid(sourceCosmosAccountResourceId, backupIdentityPrincipalId, tableDataReaderRoleDefinitionId)
  properties: {
    principalId: backupIdentityPrincipalId
    #disable-next-line use-resource-id-functions
    roleDefinitionId: tableDataReaderRoleDefinitionId
    scope: sourceCosmosAccountResourceId
  }
}

output approvedPrivateEndpointResourceId string = cosmosPrivateEndpointResourceId
output tableReaderAssignmentResourceId string = tableReaderAssignment.id
