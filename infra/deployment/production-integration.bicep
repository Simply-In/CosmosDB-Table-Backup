targetScope = 'resourceGroup'

@description('Full resource ID of the existing source Cosmos DB for Table account.')
param sourceCosmosAccountResourceId string
@description('Principal ID of the backup runtime UAMI from the backup-subscription deployment output.')
param backupIdentityPrincipalId string
@description('Name of the pending private endpoint connection created in the backup subscription.')
param cosmosPrivateEndpointName string
@description('Full resource ID of that private endpoint; used to bind approval to the expected endpoint.')
param cosmosPrivateEndpointResourceId string

var idParts = split(sourceCosmosAccountResourceId, '/')
var sourceSubscriptionId = idParts[2]
var sourceAccountName = idParts[8]

// This template deliberately runs under the source-subscription deployment identity. It creates no
// network, DNS, route, endpoint, account, key, or backup-policy resources in the source subscription.
module integration '../resources/source-integration.bicep' = {
  name: 'cosmos-table-backup-integration'
  params: {
    sourceCosmosAccountResourceId: sourceCosmosAccountResourceId
    sourceAccountName: sourceAccountName
    backupIdentityPrincipalId: backupIdentityPrincipalId
    cosmosPrivateEndpointName: cosmosPrivateEndpointName
    cosmosPrivateEndpointResourceId: cosmosPrivateEndpointResourceId
  }
}

output approvedPrivateEndpointResourceId string = integration.outputs.approvedPrivateEndpointResourceId
output tableReaderAssignmentResourceId string = integration.outputs.tableReaderAssignmentResourceId
output expectedSourceSubscriptionId string = sourceSubscriptionId
