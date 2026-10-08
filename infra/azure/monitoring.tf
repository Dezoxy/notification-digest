locals {
  # A fuzzy union still needs one operand that resolves. Both log tables are
  # absent until the first job execution, so the empty datatable keeps the
  # rule creatable and the foundation apply from failing with SEM0529.
  failure_query = <<-KQL
    union isfuzzy=true (datatable(TimeGenerated:datetime, Log_s:string)[]), ContainerAppConsoleLogs_CL, ContainerAppSystemLogs_CL
    | where TimeGenerated > ago(30m)
    | where tostring(column_ifexists("ContainerJobName_s", "")) startswith "digest-"
        or tostring(column_ifexists("ContainerAppName_s", "")) startswith "digest-"
        or tostring(column_ifexists("ContainerGroupName_s", "")) startswith "digest-"
    | where Log_s contains "cloud_job event=failure"
        or Log_s contains "cloud delivery uncertain"
        or tostring(column_ifexists("Type_s", "")) == "Error"
        or tostring(column_ifexists("Reason_s", "")) in ("ContainerCrashing", "ErrImagePull", "ImagePullBackOff", "JobExecutionFailed")
    | summarize Failures = count()
  KQL
  # Azure log-alert lookback is bounded to two days. Weekly freshness is
  # evaluated after Sunday evening through Monday evening, while the expected
  # execution is inside that lookback. Other modes are checked continuously.
  freshness_query = <<-KQL
    let LocalNow = datetime_utc_to_local(now(), "Europe/Budapest");
    let Expected = datatable(Job:string, MaxAge:timespan)
      ["daytime",20h,"overnight",26h,"evening",26h,"daily",26h,
       "weekly",26h,"positions",6h,"patreon",2h,"relay",2h,"backup",26h];
    let Success = ContainerAppConsoleLogs_CL
      | where TimeGenerated > ago(2d)
      | where tostring(column_ifexists("ContainerJobName_s", "")) startswith "digest-"
          or tostring(column_ifexists("ContainerGroupName_s", "")) startswith "digest-"
      | where Log_s contains "cloud_job event=success" or Log_s contains "cloud_job event=already_completed"
      | extend Job = extract(@"job=([a-z]+)", 1, Log_s)
      | summarize LastSuccess = max(TimeGenerated) by Job;
    Expected
      | where Job != "weekly"
          or (dayofweek(LocalNow) == 0d and hourofday(LocalNow) >= 23)
          or (dayofweek(LocalNow) == 1d and hourofday(LocalNow) < 22)
      | join kind=leftouter Success on Job
      | where isnull(LastSuccess) or now() - LastSuccess > MaxAge
      | summarize StaleJobs = count()
  KQL
}

resource "azurerm_monitor_action_group" "owner" {
  name                = "notification-digest-owner"
  resource_group_name = azurerm_resource_group.digest.name
  short_name          = "digest"
  email_receiver {
    name                    = "owner"
    email_address           = var.alert_email
    use_common_alert_schema = true
  }
  tags = local.tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "digest" {
  for_each = {
    failures  = { query = local.failure_query, column = "Failures", window = "PT30M" }
    freshness = { query = local.freshness_query, column = "StaleJobs", window = "P2D" }
  }
  name                    = "notification-digest-${each.key}"
  resource_group_name     = azurerm_resource_group.digest.name
  location                = var.location
  evaluation_frequency    = "PT15M"
  window_duration         = each.value.window
  scopes                  = [azurerm_log_analytics_workspace.digest.id]
  severity                = 2
  enabled                 = var.schedules_enabled
  auto_mitigation_enabled = true
  # Tables are created on first execution. Validate real schema/KQL in pilot
  # before enabling schedules and these two aggregate (unsplit) alerts.
  skip_query_validation = true
  criteria {
    query                   = each.value.query
    time_aggregation_method = "Maximum"
    metric_measure_column   = each.value.column
    operator                = "GreaterThan"
    threshold               = 0
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }
  action {
    action_groups = [azurerm_monitor_action_group.owner.id]
  }
  tags = local.tags
}

resource "azurerm_consumption_budget_resource_group" "digest" {
  name              = "notification-digest-monthly"
  resource_group_id = azurerm_resource_group.digest.id
  amount            = var.monthly_budget
  time_grain        = "Monthly"
  time_period { start_date = var.budget_start_date }
  dynamic "notification" {
    for_each = toset([80, 100])
    content {
      enabled        = true
      threshold      = notification.value
      threshold_type = "Actual"
      operator       = "GreaterThanOrEqualTo"
      contact_emails = [var.alert_email]
    }
  }
}
