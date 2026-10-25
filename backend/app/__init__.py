"""ReportFlow 后端。

分层依赖方向（只允许向右依赖，反向即违规）：

    contracts  <-  persistence / agents / orchestrator  <-  core(装配) / cli
"""
