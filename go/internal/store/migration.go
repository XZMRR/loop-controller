package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"strings"
	"time"
)

type queryExecer interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

type migration struct {
	version int
	name    string
	source  string
	apply   func(context.Context, queryExecer) error
}

const adoptCurrentStoreSchemaPlan = `ordered legacy column adoption:
tasks.interaction_id TEXT NOT NULL DEFAULT ''
tasks.decision_id TEXT NOT NULL DEFAULT ''
tasks.root_interaction_id TEXT NOT NULL DEFAULT ''
tasks.parent_interaction_id TEXT NOT NULL DEFAULT ''
tasks.root_task_id TEXT NOT NULL DEFAULT ''
tasks.parent_task_id TEXT NOT NULL DEFAULT ''
tasks.budget_currency TEXT NOT NULL DEFAULT ''
tasks.delegation_token TEXT NOT NULL DEFAULT ''
tasks.allowed_tools_json TEXT NOT NULL DEFAULT '[]'
tasks.allowed_capabilities_json TEXT NOT NULL DEFAULT '[]'
tasks.tenant_id TEXT NOT NULL DEFAULT ''
tasks.request_id TEXT NOT NULL DEFAULT ''
tasks.target_workload_id TEXT NOT NULL DEFAULT ''
tasks.target_instance_id TEXT NOT NULL DEFAULT ''
delegation_approvals.tenant_id TEXT NOT NULL DEFAULT ''
delegation_approvals.target_workload_id TEXT NOT NULL DEFAULT ''
delegation_approvals.target_instance_id TEXT NOT NULL DEFAULT ''
tasks.delegation_depth INTEGER NOT NULL DEFAULT 0
tasks.budget_token_count INTEGER NOT NULL DEFAULT 0
tasks.budget_payment_amount REAL NOT NULL DEFAULT 0
tasks.reserved_token_count INTEGER NOT NULL DEFAULT 0
tasks.reserved_payment_amount REAL NOT NULL DEFAULT 0
tasks.consumed_token_count INTEGER NOT NULL DEFAULT 0
tasks.consumed_payment_amount REAL NOT NULL DEFAULT 0
tasks.allow_redelegation INTEGER NOT NULL DEFAULT 0
tasks.exec_owner TEXT NOT NULL DEFAULT ''
tasks.exec_lease_expires_at INTEGER NOT NULL DEFAULT 0
tasks.deadline TEXT
lifecycle_outbox.claimed_by TEXT NOT NULL DEFAULT ''
lifecycle_outbox.claim_token TEXT NOT NULL DEFAULT ''
lifecycle_outbox.claim_expires_at INTEGER NOT NULL DEFAULT 0
approval_audit_outbox.delivery_id TEXT NOT NULL DEFAULT ''
approval_notification_outbox.destination_url TEXT NOT NULL DEFAULT ''
approval_notification_outbox.destination_digest TEXT NOT NULL DEFAULT ''
ordered backfill/index:
UPDATE approval_audit_outbox SET delivery_id='approval-audit:'||approval_id||':'||event WHERE delivery_id=''
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_audit_delivery_id ON approval_audit_outbox(delivery_id)`

// Migration 1 is released and frozen. Never change its name, source/plan, or
// behavior; add a new migration for subsequent schema changes.
var storeMigrations = []migration{{
	version: 1,
	name:    "adopt_current_store_schema",
	source:  schema + "\n-- migration plan --\n" + adoptCurrentStoreSchemaPlan,
	apply:   applyAdoptCurrentStoreSchema,
}, {
	version: 2,
	name:    "agent_registry_v2",
	source:  agentRegistryV2Schema,
	apply:   applyAgentRegistryV2,
}, {
	version: 3,
	name:    "scheduling_reservation_lifecycle",
	source:  schedulingReservationLifecycleSchema,
	apply:   applySchedulingReservationLifecycle,
}, {
	version: 4,
	name:    "outbound_assignment_worker",
	source:  outboundAssignmentWorkerSchema,
	apply:   applyOutboundAssignmentWorker,
}, {
	version: 5,
	name:    "bounded_static_dag",
	source:  boundedStaticDAGSchema,
	apply:   applyBoundedStaticDAG,
}, {
	version: 6,
	name:    "budget_aware_retry_dead_letter",
	source:  budgetAwareRetrySchema,
	apply:   applyBudgetAwareRetry,
}, {
	version: 7,
	name:    "reliable_task_event_stream",
	source:  reliableTaskEventStreamSchema,
	apply:   applyReliableTaskEventStream,
}}

const reliableTaskEventStreamSchema = `
REBUILD events with sequence INTEGER NOT NULL and schema_version INTEGER NOT NULL DEFAULT 1;
BACKFILL sequence deterministically per task ordered by legacy rowid;
CREATE UNIQUE INDEX idx_events_task_sequence ON events(task_id,sequence);
CREATE INDEX idx_events_task_unpublished_sequence ON events(task_id,published,sequence);
CREATE TABLE event_retention(task_id PRIMARY KEY,lower_sequence,next_sequence,compacted_at);
CREATE TABLE terminal_event_audit(task_id PRIMARY KEY,status,outcome,error_code,budget fields,terminal_sequence,terminal event/time,recorded_at);
`

func applyReliableTaskEventStream(ctx context.Context, db queryExecer) error {
	upgraded, err := migrationColumnExists(ctx, db, "events", "sequence")
	if err != nil {
		return err
	}
	if upgraded {
		_, err = db.ExecContext(ctx, `
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_task_sequence ON events(task_id,sequence);
CREATE INDEX IF NOT EXISTS idx_events_task_published ON events(task_id,published);
CREATE INDEX IF NOT EXISTS idx_events_task_unpublished_sequence ON events(task_id,published,sequence);
CREATE TABLE IF NOT EXISTS event_retention (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
 lower_sequence INTEGER NOT NULL DEFAULT 1 CHECK(lower_sequence>=1),
 next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence>=lower_sequence),
 compacted_at TEXT
);
INSERT OR IGNORE INTO event_retention(task_id,lower_sequence,next_sequence)
 SELECT task_id,1,COALESCE(MAX(sequence),0)+1 FROM events GROUP BY task_id;
INSERT OR IGNORE INTO event_retention(task_id,lower_sequence,next_sequence) SELECT task_id,1,1 FROM tasks;
CREATE TABLE IF NOT EXISTS terminal_event_audit (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
 terminal_status TEXT NOT NULL, outcome_json TEXT, error_code TEXT NOT NULL DEFAULT '',
 budget_token_count INTEGER NOT NULL DEFAULT 0, budget_payment_amount REAL NOT NULL DEFAULT 0,
 budget_currency TEXT NOT NULL DEFAULT '', terminal_sequence INTEGER NOT NULL,
 terminal_event_type TEXT NOT NULL, terminal_event_payload TEXT NOT NULL,
 terminal_event_at TEXT NOT NULL, recorded_at TEXT NOT NULL
);`)
		return err
	}
	_, err = db.ExecContext(ctx, `
DROP TABLE IF EXISTS events_v7;
CREATE TABLE events_v7 (
 event_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 published_at TEXT NOT NULL,
 published INTEGER NOT NULL DEFAULT 0,
 sequence INTEGER NOT NULL CHECK(sequence>=1),
 schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version>=1),
 UNIQUE(task_id,sequence)
);
INSERT INTO events_v7(event_id,task_id,event_type,payload_json,published_at,published,sequence,schema_version)
 SELECT event_id,task_id,event_type,payload_json,published_at,published,
        ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY rowid),1
 FROM events;
DROP TABLE events;
ALTER TABLE events_v7 RENAME TO events;
CREATE UNIQUE INDEX idx_events_task_sequence ON events(task_id,sequence);
CREATE INDEX idx_events_task_published ON events(task_id,published);
CREATE INDEX idx_events_task_unpublished_sequence ON events(task_id,published,sequence);
DROP TABLE IF EXISTS event_retention;
DROP TABLE IF EXISTS terminal_event_audit;
CREATE TABLE event_retention (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
 lower_sequence INTEGER NOT NULL DEFAULT 1 CHECK(lower_sequence>=1),
 next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence>=lower_sequence),
 compacted_at TEXT
);
INSERT INTO event_retention(task_id,lower_sequence,next_sequence)
 SELECT task_id,1,COALESCE(MAX(sequence),0)+1 FROM events GROUP BY task_id;
INSERT OR IGNORE INTO event_retention(task_id,lower_sequence,next_sequence)
 SELECT task_id,1,1 FROM tasks;
CREATE TABLE terminal_event_audit (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
 terminal_status TEXT NOT NULL,
 outcome_json TEXT,
 error_code TEXT NOT NULL DEFAULT '',
 budget_token_count INTEGER NOT NULL DEFAULT 0,
 budget_payment_amount REAL NOT NULL DEFAULT 0,
 budget_currency TEXT NOT NULL DEFAULT '',
 terminal_sequence INTEGER NOT NULL,
 terminal_event_type TEXT NOT NULL,
 terminal_event_payload TEXT NOT NULL,
 terminal_event_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
`)
	return err
}

const budgetAwareRetrySchema = `
REBUILD task_assignments, execution_attempts, delegation_dispatch_outbox and agent_capacity_reservations for route history;
ADD retry_policy_json, replay_count, route_attempt, supersedes_assignment_id, dispatch_disposition and failover_transition_id;
CREATE partial unique active route and failover audit indexes;
`

func applyBudgetAwareRetry(ctx context.Context, db queryExecer) error {
	_, err := db.ExecContext(ctx, `
CREATE TABLE task_assignments_v6 (
 assignment_id TEXT PRIMARY KEY, assignment_kind TEXT NOT NULL DEFAULT 'target_execution', tenant_id TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(task_id), agent_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','claimed','dispatched','executing','settled','retry_wait','dead_letter','cancelled','superseded')),
 revision INTEGER NOT NULL CHECK(revision>=1), route_attempt INTEGER NOT NULL DEFAULT 1 CHECK(route_attempt>=1),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0), execution_fence INTEGER NOT NULL DEFAULT 0 CHECK(execution_fence>=0),
 lease_owner TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', lease_expires_at INTEGER NOT NULL DEFAULT 0,
 not_before TEXT, deadline TEXT, delivery_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL DEFAULT '',
 retry_policy_json TEXT NOT NULL DEFAULT '{"max_attempts":5,"initial_backoff":1000000000,"max_backoff":60000000000,"backoff_multiplier":2,"jitter":0,"retryable_failure_classes":["pre_dispatch_transient"],"attempt_budget_limit":{"token_count":0,"payment_amount":0}}',
 replay_count INTEGER NOT NULL DEFAULT 0, supersedes_assignment_id TEXT NOT NULL DEFAULT '',
 dispatch_disposition TEXT NOT NULL DEFAULT '', failover_transition_id TEXT NOT NULL DEFAULT '',
 failure_class TEXT NOT NULL DEFAULT '', result_status TEXT NOT NULL DEFAULT '', outcome_json TEXT, error_code TEXT NOT NULL DEFAULT '',
 consumed_token_count INTEGER NOT NULL DEFAULT 0, consumed_payment_amount REAL NOT NULL DEFAULT 0, consumed_currency TEXT NOT NULL DEFAULT '',
 execution_receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO task_assignments_v6(assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at)
 SELECT assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at FROM task_assignments;
CREATE TABLE execution_attempts_v6 AS SELECT * FROM execution_attempts;
CREATE TABLE delegation_dispatch_outbox_v6 (
 delivery_id TEXT PRIMARY KEY, approval_id TEXT REFERENCES delegation_approvals(approval_id), task_id TEXT NOT NULL REFERENCES tasks(task_id),
 assignment_id TEXT UNIQUE REFERENCES task_assignments_v6(assignment_id), entrypoint_json TEXT NOT NULL, payload_json TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, delivered_at TEXT, terminal_at TEXT,
 failure_class TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '', claimed_by TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', claim_expires_at INTEGER NOT NULL DEFAULT 0
);
INSERT INTO delegation_dispatch_outbox_v6 SELECT * FROM delegation_dispatch_outbox;
CREATE TABLE agent_capacity_reservations_v6 (
 reservation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(agent_id), task_id TEXT NOT NULL,
 units INTEGER NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, assignment_id TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released','uncertain')), revision INTEGER NOT NULL DEFAULT 1,
 release_reason TEXT NOT NULL DEFAULT '', released_at TEXT
);
INSERT INTO agent_capacity_reservations_v6 SELECT * FROM agent_capacity_reservations;
DROP TABLE execution_attempts;
DROP TABLE delegation_dispatch_outbox;
DROP TABLE agent_capacity_reservations;
DROP TABLE task_assignments;
ALTER TABLE task_assignments_v6 RENAME TO task_assignments;
CREATE TABLE execution_attempts (
 assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id), tenant_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 attempt INTEGER NOT NULL CHECK(attempt>0), execution_fence INTEGER NOT NULL CHECK(execution_fence>0),
 state TEXT NOT NULL CHECK(state IN ('claimed','dispatched','executing','settled','cancelled','superseded')),
 lease_owner TEXT NOT NULL, claim_token TEXT NOT NULL, lease_expires_at INTEGER NOT NULL, failure_class TEXT NOT NULL DEFAULT '',
 result_status TEXT NOT NULL DEFAULT '', outcome_json TEXT, error_code TEXT NOT NULL DEFAULT '', consumed_token_count INTEGER NOT NULL DEFAULT 0,
 consumed_payment_amount REAL NOT NULL DEFAULT 0, consumed_currency TEXT NOT NULL DEFAULT '', execution_receipt_json TEXT,
 started_at TEXT, finished_at TEXT, PRIMARY KEY(assignment_id,attempt), UNIQUE(assignment_id,execution_fence)
);
INSERT INTO execution_attempts SELECT * FROM execution_attempts_v6;
DROP TABLE execution_attempts_v6;
ALTER TABLE delegation_dispatch_outbox_v6 RENAME TO delegation_dispatch_outbox;
ALTER TABLE agent_capacity_reservations_v6 RENAME TO agent_capacity_reservations;
CREATE UNIQUE INDEX idx_task_assignments_tenant_idempotency ON task_assignments(tenant_id,idempotency_key) WHERE idempotency_key<>'';
CREATE UNIQUE INDEX idx_task_assignments_active_task_kind ON task_assignments(tenant_id,task_id,assignment_kind) WHERE state NOT IN ('settled','cancelled','superseded');
CREATE UNIQUE INDEX idx_task_assignments_failover_transition ON task_assignments(failover_transition_id) WHERE failover_transition_id<>'';
CREATE INDEX idx_task_assignments_due ON task_assignments(state,not_before,lease_expires_at,assignment_id);
CREATE INDEX idx_task_assignments_kind_due ON task_assignments(assignment_kind,state,not_before,lease_expires_at,assignment_id);
CREATE INDEX idx_task_assignments_dead_letter_tenant ON task_assignments(tenant_id,state,updated_at,assignment_id);
CREATE INDEX idx_task_assignments_route_history ON task_assignments(tenant_id,task_id,assignment_kind,route_attempt);
CREATE UNIQUE INDEX idx_delegation_dispatch_task_assignment ON delegation_dispatch_outbox(task_id,assignment_id);
CREATE INDEX idx_delegation_dispatch_due ON delegation_dispatch_outbox(delivered_at,next_attempt_at,delivery_id);
CREATE INDEX idx_capacity_reservations_agent_expiry ON agent_capacity_reservations(tenant_id,agent_id,expires_at);
CREATE UNIQUE INDEX idx_capacity_reservations_assignment ON agent_capacity_reservations(assignment_id) WHERE assignment_id<>'';
CREATE INDEX idx_capacity_reservations_active ON agent_capacity_reservations(tenant_id,agent_id,state);
DROP TABLE IF EXISTS assignment_retry_events;
CREATE TABLE assignment_retry_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('retry_scheduled','dead_lettered','replayed','failed_over')),
 failure_class TEXT NOT NULL DEFAULT '', dispatch_disposition TEXT NOT NULL DEFAULT '', failover_transition_id TEXT NOT NULL DEFAULT '',
 superseded_assignment_id TEXT, not_before TEXT, expected_revision INTEGER NOT NULL, actor TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', correlation_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX idx_assignment_retry_events_assignment ON assignment_retry_events(tenant_id,assignment_id,event_id);
`)
	return err
}

const boundedStaticDAGSchema = `
CREATE TABLE task_graphs (
 dag_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, root_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
 status TEXT NOT NULL CHECK(status IN ('running','cancelling','completed','failed','cancelled','outcome_unknown')),
 max_parallelism INTEGER NOT NULL CHECK(max_parallelism BETWEEN 1 AND 32), deadline TEXT,
 budget_token_count INTEGER NOT NULL CHECK(budget_token_count>=0), budget_payment_amount REAL NOT NULL CHECK(budget_payment_amount>=0), budget_currency TEXT NOT NULL,
 reserved_token_count INTEGER NOT NULL DEFAULT 0 CHECK(reserved_token_count>=0), reserved_payment_amount REAL NOT NULL DEFAULT 0 CHECK(reserved_payment_amount>=0),
 consumed_token_count INTEGER NOT NULL DEFAULT 0 CHECK(consumed_token_count>=0), consumed_payment_amount REAL NOT NULL DEFAULT 0 CHECK(consumed_payment_amount>=0),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1), idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, cancel_reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, terminal_at TEXT,
 CHECK(reserved_token_count+consumed_token_count<=budget_token_count), CHECK(reserved_payment_amount+consumed_payment_amount<=budget_payment_amount),
 UNIQUE(tenant_id,idempotency_key)
);
CREATE INDEX idx_task_graphs_tenant_created ON task_graphs(tenant_id,created_at,dag_id);
CREATE TABLE task_graph_nodes (
 dag_id TEXT NOT NULL REFERENCES task_graphs(dag_id) ON DELETE CASCADE, node_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id), tenant_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','ready','running','completed','failed','cancelled','skipped','outcome_unknown')),
 failure_policy TEXT NOT NULL CHECK(failure_policy IN ('fail_fast','continue_independent')), retry_policy_json TEXT NOT NULL, scheduling_request_json TEXT NOT NULL,
 budget_token_count INTEGER NOT NULL CHECK(budget_token_count>=0), budget_payment_amount REAL NOT NULL CHECK(budget_payment_amount>=0), budget_currency TEXT NOT NULL,
 depth INTEGER NOT NULL CHECK(depth BETWEEN 0 AND 32), topo_order INTEGER NOT NULL CHECK(topo_order>=0), revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, terminal_at TEXT, PRIMARY KEY(dag_id,node_id), UNIQUE(dag_id,topo_order)
);
CREATE INDEX idx_task_graph_nodes_ready ON task_graph_nodes(status,dag_id,node_id);
CREATE TABLE task_graph_edges (
 dag_id TEXT NOT NULL REFERENCES task_graphs(dag_id) ON DELETE CASCADE, from_node_id TEXT NOT NULL, to_node_id TEXT NOT NULL,
 PRIMARY KEY(dag_id,from_node_id,to_node_id), CHECK(from_node_id<>to_node_id),
 FOREIGN KEY(dag_id,from_node_id) REFERENCES task_graph_nodes(dag_id,node_id) ON DELETE CASCADE,
 FOREIGN KEY(dag_id,to_node_id) REFERENCES task_graph_nodes(dag_id,node_id) ON DELETE CASCADE
);
CREATE INDEX idx_task_graph_edges_to ON task_graph_edges(dag_id,to_node_id,from_node_id);
CREATE TABLE task_graph_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, dag_id TEXT NOT NULL REFERENCES task_graphs(dag_id) ON DELETE CASCADE,
 node_id TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX idx_task_graph_events_graph ON task_graph_events(dag_id,event_id);
CREATE TABLE dag_wakeup_outbox (
 dag_id TEXT NOT NULL, node_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','claimed','acked')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1), claimed_by TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', claim_expires_at INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(dag_id,node_id),
 FOREIGN KEY(dag_id,node_id) REFERENCES task_graph_nodes(dag_id,node_id) ON DELETE CASCADE
);
CREATE INDEX idx_dag_wakeup_due ON dag_wakeup_outbox(state,claim_expires_at,dag_id,node_id);
`

func applyBoundedStaticDAG(ctx context.Context, db queryExecer) error {
	ddl := strings.ReplaceAll(boundedStaticDAGSchema, "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
	ddl = strings.ReplaceAll(ddl, "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
	_, err := db.ExecContext(ctx, ddl)
	return err
}

const outboundAssignmentWorkerSchema = `
ALTER TABLE task_assignments ADD COLUMN assignment_kind TEXT NOT NULL DEFAULT 'target_execution';
REBUILD delegation_dispatch_outbox WITH nullable approval_id and assignment payload linkage;
CREATE INDEX idx_task_assignments_kind_due ON task_assignments(assignment_kind,state,not_before,lease_expires_at,assignment_id);
CREATE UNIQUE INDEX idx_delegation_dispatch_assignment ON delegation_dispatch_outbox(assignment_id) WHERE assignment_id IS NOT NULL;
`

func applyOutboundAssignmentWorker(ctx context.Context, db queryExecer) error {
	if err := ensureColumn(ctx, db, "task_assignments", "assignment_kind", "TEXT NOT NULL DEFAULT 'target_execution'"); err != nil {
		return err
	}
	_, err := db.ExecContext(ctx, `
ALTER TABLE delegation_dispatch_outbox RENAME TO delegation_dispatch_outbox_legacy;
CREATE TABLE delegation_dispatch_outbox (
 delivery_id TEXT PRIMARY KEY, approval_id TEXT UNIQUE REFERENCES delegation_approvals(approval_id),
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id), assignment_id TEXT UNIQUE REFERENCES task_assignments(assignment_id),
 entrypoint_json TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL, delivered_at TEXT, terminal_at TEXT, failure_class TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT '', claimed_by TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', claim_expires_at INTEGER NOT NULL DEFAULT 0
);
INSERT INTO delegation_dispatch_outbox(delivery_id,approval_id,task_id,entrypoint_json,payload_json,attempts,next_attempt_at,delivered_at,last_error,claimed_by,claim_token,claim_expires_at)
 SELECT delivery_id,approval_id,task_id,entrypoint_json,payload_json,attempts,next_attempt_at,delivered_at,last_error,claimed_by,claim_token,claim_expires_at FROM delegation_dispatch_outbox_legacy;
DROP TABLE delegation_dispatch_outbox_legacy;
UPDATE task_assignments SET assignment_kind='target_execution' WHERE assignment_kind='';
CREATE INDEX IF NOT EXISTS idx_task_assignments_kind_due ON task_assignments(assignment_kind,state,not_before,lease_expires_at,assignment_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_dispatch_assignment ON delegation_dispatch_outbox(assignment_id) WHERE assignment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_delegation_dispatch_due ON delegation_dispatch_outbox(delivered_at,next_attempt_at,delivery_id);`)
	return err
}

const schedulingReservationLifecycleSchema = `
ALTER TABLE scheduling_decisions ADD COLUMN request_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_capacity_reservations ADD COLUMN assignment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_capacity_reservations ADD COLUMN state TEXT NOT NULL DEFAULT 'held';
ALTER TABLE agent_capacity_reservations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_capacity_reservations ADD COLUMN release_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_capacity_reservations ADD COLUMN released_at TEXT;
CREATE UNIQUE INDEX idx_scheduling_decisions_request_unique ON scheduling_decisions(tenant_id, request_id);
CREATE INDEX idx_capacity_reservations_active ON agent_capacity_reservations(tenant_id, agent_id, state);
`

func applySchedulingReservationLifecycle(ctx context.Context, db queryExecer) error {
	for _, column := range []struct{ table, name, definition string }{
		{"scheduling_decisions", "request_hash", "TEXT NOT NULL DEFAULT ''"},
		{"agent_capacity_reservations", "assignment_id", "TEXT NOT NULL DEFAULT ''"},
		{"agent_capacity_reservations", "state", "TEXT NOT NULL DEFAULT 'held'"},
		{"agent_capacity_reservations", "revision", "INTEGER NOT NULL DEFAULT 1"},
		{"agent_capacity_reservations", "release_reason", "TEXT NOT NULL DEFAULT ''"},
		{"agent_capacity_reservations", "released_at", "TEXT"},
	} {
		if err := ensureColumn(ctx, db, column.table, column.name, column.definition); err != nil {
			return err
		}
	}
	_, err := db.ExecContext(ctx, `CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduling_decisions_request_unique ON scheduling_decisions(tenant_id, request_id); CREATE INDEX IF NOT EXISTS idx_capacity_reservations_active ON agent_capacity_reservations(tenant_id, agent_id, state);`)
	return err
}

const agentRegistryV2Schema = `
ALTER TABLE agents ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN tools_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agents ADD COLUMN protocol_capabilities_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agents ADD COLUMN security_capabilities_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agents ADD COLUMN expected_workload_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN labels_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agents ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN weight INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN schedulable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN source_type TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE agents ADD COLUMN source_id TEXT NOT NULL DEFAULT 'migration-1';
ALTER TABLE agents ADD COLUMN external_revision TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN generation INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agents ADD COLUMN resource_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agents ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN deleted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_agents_tenant_active ON agents(tenant_id, agent_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agents_source ON agents(tenant_id, source_type, source_id);
CREATE TABLE agent_status (
 tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, observed_generation INTEGER NOT NULL,
 health TEXT NOT NULL, current_load INTEGER NOT NULL, available_capacity INTEGER NOT NULL,
 draining INTEGER NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 status_revision INTEGER NOT NULL, reported_by TEXT NOT NULL,
 PRIMARY KEY(tenant_id, agent_id), FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
);
CREATE INDEX idx_agent_status_expiry ON agent_status(expires_at);
CREATE TABLE discovery_source_state (
 tenant_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
 last_revision TEXT NOT NULL DEFAULT '', last_success_at TEXT, last_attempt_at TEXT NOT NULL,
 stale INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', resource_version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id, source_type, source_id)
);
CREATE TABLE scheduling_decisions (
 decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,
 selected_agent_id TEXT NOT NULL DEFAULT '', algorithm_version TEXT NOT NULL,
 candidates_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_scheduling_decisions_request ON scheduling_decisions(tenant_id, request_id);
CREATE TABLE agent_capacity_reservations (
 reservation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
 units INTEGER NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
);
CREATE INDEX idx_capacity_reservations_agent_expiry ON agent_capacity_reservations(tenant_id, agent_id, expires_at);
CREATE UNIQUE INDEX idx_capacity_reservations_task ON agent_capacity_reservations(tenant_id, task_id);
`

const agentRegistryV2Objects = `
CREATE INDEX IF NOT EXISTS idx_agents_tenant_active ON agents(tenant_id, agent_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agents_source ON agents(tenant_id, source_type, source_id);
CREATE TABLE IF NOT EXISTS agent_status (tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, observed_generation INTEGER NOT NULL, health TEXT NOT NULL, current_load INTEGER NOT NULL, available_capacity INTEGER NOT NULL, draining INTEGER NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL, status_revision INTEGER NOT NULL, reported_by TEXT NOT NULL, PRIMARY KEY(tenant_id, agent_id), FOREIGN KEY(agent_id) REFERENCES agents(agent_id));
CREATE INDEX IF NOT EXISTS idx_agent_status_expiry ON agent_status(expires_at);
CREATE TABLE IF NOT EXISTS discovery_source_state (tenant_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL, last_revision TEXT NOT NULL DEFAULT '', last_success_at TEXT, last_attempt_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', resource_version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(tenant_id, source_type, source_id));
CREATE TABLE IF NOT EXISTS scheduling_decisions (decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL, selected_agent_id TEXT NOT NULL DEFAULT '', algorithm_version TEXT NOT NULL, candidates_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_scheduling_decisions_request ON scheduling_decisions(tenant_id, request_id);
CREATE TABLE IF NOT EXISTS agent_capacity_reservations (reservation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL, units INTEGER NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(agent_id) REFERENCES agents(agent_id));
CREATE INDEX IF NOT EXISTS idx_capacity_reservations_agent_expiry ON agent_capacity_reservations(tenant_id, agent_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_reservations_task ON agent_capacity_reservations(tenant_id, task_id);
`

func applyAgentRegistryV2(ctx context.Context, db queryExecer) error {
	columns := []struct{ name, definition string }{
		{"tenant_id", "TEXT NOT NULL DEFAULT ''"}, {"tools_json", "TEXT NOT NULL DEFAULT '[]'"},
		{"protocol_capabilities_json", "TEXT NOT NULL DEFAULT '[]'"}, {"security_capabilities_json", "TEXT NOT NULL DEFAULT '[]'"},
		{"expected_workload_id", "TEXT NOT NULL DEFAULT ''"}, {"labels_json", "TEXT NOT NULL DEFAULT '{}'"},
		{"priority", "INTEGER NOT NULL DEFAULT 0"}, {"weight", "INTEGER NOT NULL DEFAULT 0"},
		{"max_concurrency", "INTEGER NOT NULL DEFAULT 0"}, {"schedulable", "INTEGER NOT NULL DEFAULT 0"},
		{"source_type", "TEXT NOT NULL DEFAULT 'legacy'"}, {"source_id", "TEXT NOT NULL DEFAULT 'migration-1'"},
		{"external_revision", "TEXT NOT NULL DEFAULT ''"}, {"generation", "INTEGER NOT NULL DEFAULT 1"},
		{"resource_version", "INTEGER NOT NULL DEFAULT 1"}, {"created_at", "TEXT NOT NULL DEFAULT ''"},
		{"updated_at", "TEXT NOT NULL DEFAULT ''"}, {"deleted_at", "TEXT"},
	}
	for _, column := range columns {
		if err := ensureColumn(ctx, db, "agents", column.name, column.definition); err != nil {
			return err
		}
	}
	if _, err := db.ExecContext(ctx, agentRegistryV2Objects); err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := db.ExecContext(ctx, `UPDATE agents SET tenant_id=COALESCE(tenant_id,''), source_type='legacy', source_id='migration-1', schedulable=0, generation=1, resource_version=1, created_at=?, updated_at=?`, now, now)
	if err != nil {
		return err
	}
	_, err = db.ExecContext(ctx, `INSERT OR IGNORE INTO agent_status(tenant_id,agent_id,observed_generation,health,current_load,available_capacity,draining,last_seen_at,expires_at,status_revision,reported_by) SELECT tenant_id,agent_id,generation,'expired',0,0,0,?,?,1,'migration-1' FROM agents`, now, now)
	return err
}

func migrationColumnExists(ctx context.Context, db queryExecer, table, column string) (bool, error) {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info("+table+")")
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var cid, notNull, primaryKey int
		var name, columnType string
		var defaultValue any
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return false, err
		}
		if name == column {
			return true, nil
		}
	}
	return false, rows.Err()
}

func migrationChecksum(m migration) string {
	// The serialized identity is deliberately ordered and includes every frozen
	// input. Do not change it for an applied migration.
	identity := fmt.Sprintf("version=%d\nname=%s\nsource=%s", m.version, m.name, m.source)
	sum := sha256.Sum256([]byte(identity))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func runMigrations(ctx context.Context, businessDSN string, migrations []migration) error {
	migrationDB, err := sql.Open("sqlite", businessDSN+"&_txlock=immediate")
	if err != nil {
		return fmt.Errorf("open migration sqlite: %w", err)
	}
	defer migrationDB.Close()
	migrationDB.SetMaxOpenConns(1)

	var tx *sql.Tx
	for {
		tx, err = migrationDB.BeginTx(ctx, nil)
		if err == nil {
			break
		}
		if !strings.Contains(err.Error(), "SQLITE_BUSY") && !strings.Contains(err.Error(), "database is locked") {
			return fmt.Errorf("begin migration transaction: %w", err)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("begin migration transaction: %w", ctx.Err())
		case <-time.After(10 * time.Millisecond):
		}
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		name TEXT NOT NULL,
		checksum TEXT NOT NULL,
		applied_at TEXT NOT NULL
	)`); err != nil {
		return fmt.Errorf("create schema migrations: %w", err)
	}

	rows, err := tx.QueryContext(ctx, `SELECT version, name, checksum FROM schema_migrations ORDER BY version`)
	if err != nil {
		return fmt.Errorf("read schema migrations: %w", err)
	}
	type appliedMigration struct {
		version  int
		name     string
		checksum string
	}
	var applied []appliedMigration
	for rows.Next() {
		var item appliedMigration
		if err := rows.Scan(&item.version, &item.name, &item.checksum); err != nil {
			rows.Close()
			return fmt.Errorf("scan schema migration: %w", err)
		}
		applied = append(applied, item)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("read schema migrations: %w", err)
	}
	if err := rows.Close(); err != nil {
		return fmt.Errorf("close schema migration rows: %w", err)
	}

	for i, item := range applied {
		expectedVersion := i + 1
		if item.version != expectedVersion {
			return fmt.Errorf("schema migration version gap: expected %d, found %d", expectedVersion, item.version)
		}
		if i >= len(migrations) {
			return fmt.Errorf("unknown schema migration version %d", item.version)
		}
		expected := migrations[i]
		if expected.version != expectedVersion {
			return fmt.Errorf("invalid migration plan: expected version %d, found %d", expectedVersion, expected.version)
		}
		checksum := migrationChecksum(expected)
		if item.name != expected.name || item.checksum != checksum {
			return fmt.Errorf("schema migration %d identity mismatch", item.version)
		}
	}

	for i := len(applied); i < len(migrations); i++ {
		m := migrations[i]
		if m.version != i+1 {
			return fmt.Errorf("invalid migration plan: expected version %d, found %d", i+1, m.version)
		}
		if err := m.apply(ctx, tx); err != nil {
			return fmt.Errorf("apply schema migration %d %s: %w", m.version, m.name, err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES(?, ?, ?, ?)`, m.version, m.name, migrationChecksum(m), time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
			return fmt.Errorf("record schema migration %d: %w", m.version, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit schema migrations: %w", err)
	}
	return nil
}

func applyAdoptCurrentStoreSchema(ctx context.Context, db queryExecer) error {
	if _, err := db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("create schema: %w", err)
	}
	for _, column := range []string{"interaction_id", "decision_id", "root_interaction_id", "parent_interaction_id", "root_task_id", "parent_task_id", "budget_currency", "delegation_token", "allowed_tools_json", "allowed_capabilities_json", "tenant_id", "request_id", "target_workload_id", "target_instance_id"} {
		if err := ensureTaskTextColumn(ctx, db, column); err != nil {
			return err
		}
	}
	for _, column := range []string{"tenant_id", "target_workload_id", "target_instance_id"} {
		if err := ensureColumn(ctx, db, "delegation_approvals", column, "TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	for _, column := range []string{"delegation_depth", "budget_token_count", "budget_payment_amount", "reserved_token_count", "reserved_payment_amount", "consumed_token_count", "consumed_payment_amount", "allow_redelegation", "exec_owner", "exec_lease_expires_at"} {
		if err := ensureTaskColumn(ctx, db, column); err != nil {
			return err
		}
	}
	if err := ensureColumn(ctx, db, "tasks", "deadline", "TEXT"); err != nil {
		return err
	}
	for _, column := range []string{"claimed_by", "claim_token", "claim_expires_at"} {
		if err := ensureOutboxColumn(ctx, db, column); err != nil {
			return err
		}
	}
	if err := ensureColumn(ctx, db, "approval_audit_outbox", "delivery_id", "TEXT NOT NULL DEFAULT ''"); err != nil {
		return err
	}
	for _, column := range []string{"destination_url", "destination_digest"} {
		if err := ensureColumn(ctx, db, "approval_notification_outbox", column, "TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	if _, err := db.ExecContext(ctx, `UPDATE approval_audit_outbox SET delivery_id='approval-audit:'||approval_id||':'||event WHERE delivery_id=''`); err != nil {
		return fmt.Errorf("backfill approval audit delivery IDs: %w", err)
	}
	if _, err := db.ExecContext(ctx, `CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_audit_delivery_id ON approval_audit_outbox(delivery_id)`); err != nil {
		return fmt.Errorf("index approval audit delivery IDs: %w", err)
	}
	return nil
}
