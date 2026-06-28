#input_type_name: FinalizeDeploymentInput
#output_type_name: FinalizeDeploymentResult
#function_name: finalize_deployment

"""Commit a Workflow-B approval decision (M3.7).

The human FORM in the ``deploy_approval`` workflow captures ``approved`` + ``notes``; this
function does the coordinated, auditable write that follows. It reads the *canonical*
``approval_requests`` row (never trusting the form echo) and then, branching on ``approved``:

* APPROVE → issue a ``start`` command on the ``commands`` table for the deployment's worker, and
  mark the deployment + request + strategy approved. **TEST-8:** issuing a command row is the pod's
  only role — the worker polls ``commands`` and is the *sole* executor; the pod never trades.
* REJECT → record the rejection on the request / deployment / strategy; no command is issued.

Idempotency-minded: the command is emitted exactly once per approval (one FORM submit → one run →
one ``start`` row). Runs as the invoking (approving) user, so it stamps the decision with their id.
"""

from datetime import UTC, datetime

from lemma_sdk import FunctionContext, Pod
from pydantic import BaseModel

# The paper worker that runs the Delta-testnet soak; the command's default target when the
# deployment row carries no worker_id of its own (mirrors config/paper.yaml `worker_id`).
DEFAULT_WORKER_ID = "alpha-paper-1"


class FinalizeDeploymentInput(BaseModel):
    record_id: str  # the approval_requests row id (start.metadata.record_id)
    approved: bool  # the human's FORM decision
    notes: str | None = None


class FinalizeDeploymentResult(BaseModel):
    decision: str  # "approved" | "rejected"
    deployment_id: str
    command_id: str | None = None  # the issued `start` command (approve only)


async def finalize_deployment(
    ctx: FunctionContext, data: FinalizeDeploymentInput
) -> FinalizeDeploymentResult:
    pod = Pod.from_env()
    now = datetime.now(UTC).isoformat()
    user_id = ctx.user_id

    req = pod.table("approval_requests").get(data.record_id)
    deployment_id = str(req["deployment_id"])
    strategy_id = req.get("strategy_id")
    worker_id = DEFAULT_WORKER_ID
    dep = pod.table("deployments").get(deployment_id)
    if dep.get("worker_id"):
        worker_id = str(dep["worker_id"])

    if not data.approved:
        # Record the rejection; issue nothing. The deployment goes to `stopped`.
        pod.table("approval_requests").update(
            data.record_id,
            {"status": "rejected", "decided_by": user_id, "decided_at": now, "notes": data.notes},
        )
        pod.table("deployments").update(deployment_id, {"status": "stopped"})
        if strategy_id:
            pod.table("strategies").update(str(strategy_id), {"status": "rejected"})
        return FinalizeDeploymentResult(decision="rejected", deployment_id=deployment_id)

    # APPROVE — issue the worker `start` command (TEST-8: the pod issues; the worker executes).
    command = pod.table("commands").create(
        {
            "kind": "start",
            "status": "pending",
            "worker_id": worker_id,
            "deployment_id": deployment_id,
            "issued_by": user_id,
            "payload": {
                "source": "workflow:deploy_approval",
                "approval_request_id": data.record_id,
                "strategy": req.get("strategy_name"),
                "venue": req.get("venue"),
                "mode": req.get("mode") or "paper",
            },
        }
    )
    command_id = str(command["id"])

    pod.table("deployments").update(
        deployment_id,
        {"status": "approved", "approved_by": user_id, "approved_at": now, "worker_id": worker_id},
    )
    pod.table("approval_requests").update(
        data.record_id,
        {"status": "approved", "decided_by": user_id, "decided_at": now, "notes": data.notes},
    )
    if strategy_id:
        pod.table("strategies").update(str(strategy_id), {"status": "approved"})

    return FinalizeDeploymentResult(
        decision="approved", deployment_id=deployment_id, command_id=command_id
    )
