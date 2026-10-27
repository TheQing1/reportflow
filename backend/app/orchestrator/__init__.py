"""编排层对外出口：只暴露任务级 API。

节点级实现（StateGraph 构建、节点执行器）留在 graph_langgraph 里，
外部不应该 import 那些内部类型——换编排框架时它们必然全部重写。
"""

from .runner import RunOutcome, RunResult, TaskRunner, checkpoint_path_for

__all__ = ["RunOutcome", "RunResult", "TaskRunner", "checkpoint_path_for"]
