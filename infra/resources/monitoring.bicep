targetScope = 'resourceGroup'

param location string
param prefix string
param tags object
param workspaceResourceId string
param restoreScheduleEnabled bool
param alertEmails array

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${prefix}-backup-ops'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take('${prefix}backup', 12)
    enabled: true
    emailReceivers: [
      for (address, index) in alertEmails: {
        name: 'backup-${index}'
        emailAddress: address
        useCommonAlertSchema: true
      }
    ]
  }
}

// Native scheduled-query alerts are used because there is no AVM resource module for this resource.
// The application must emit `backup.completed` only after committing the authenticated final manifest,
// and `backup.failed` on a failed execution. Until then these alerts intentionally remain noisy.
resource failureAlert 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: '${prefix}-backup-failure'
  location: location
  kind: 'LogAlert'
  tags: tags
  properties: {
    displayName: 'Cosmos Table backup failure'
    description: 'A backup execution emitted backup.failed.'
    enabled: true
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [ workspaceResourceId ]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where TimeGenerated > ago(10m) | where Message has "backup.failed"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

resource deadmanAlert 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: '${prefix}-backup-deadman'
  location: location
  kind: 'LogAlert'
  tags: tags
  properties: {
    displayName: 'No committed Cosmos Table backup for 26 hours'
    description: 'No backup.completed event tied to a committed final manifest was observed.'
    enabled: true
    severity: 0
    evaluationFrequency: 'PT1H'
    windowSize: 'P1D'
    overrideQueryTimeRange: 'PT48H'
    scopes: [ workspaceResourceId ]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where TimeGenerated > ago(26h) | where Message has "backup.completed"'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

// Restore alerting is event based so the application must emit restore.failed on any failed validation
// and restore.completed only after data integrity validation succeeds against the isolated target.
resource restoreFailureAlert 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: '${prefix}-restore-failure'
  location: location
  kind: 'LogAlert'
  tags: tags
  properties: {
    displayName: 'Cosmos Table restore validation failure'
    description: 'A restore-validation execution emitted restore.failed.'
    enabled: true
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [ workspaceResourceId ]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where TimeGenerated > ago(10m) | where Message has "restore.failed"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

resource restoreDeadmanAlert 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = if (restoreScheduleEnabled) {
  name: '${prefix}-restore-deadman'
  location: location
  kind: 'LogAlert'
  tags: tags
  properties: {
    displayName: 'No successful monthly Cosmos Table restore validation'
    description: 'No restore.completed event was observed in the last 35 days.'
    enabled: true
    severity: 0
    evaluationFrequency: 'P1D'
    windowSize: 'P1D'
    overrideQueryTimeRange: 'P35D'
    scopes: [ workspaceResourceId ]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where TimeGenerated > ago(35d) | where Message has "restore.completed"'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [ actionGroup.id ] }
  }
}
