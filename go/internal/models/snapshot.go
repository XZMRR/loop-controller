package models

import "time"

const TaskSnapshotSchemaVersion = "p54-08.v1"

type AssignmentSnapshot struct {
	Assignment TaskAssignment     `json:"assignment"`
	Attempts   []ExecutionAttempt `json:"attempts"`
}

type TaskSnapshot struct {
	SchemaVersion string               `json:"schema_version"`
	ReadAt        time.Time            `json:"read_at"`
	Task          Task                 `json:"task"`
	Graph         *TaskGraph           `json:"graph,omitempty"`
	Assignments   []AssignmentSnapshot `json:"assignments"`
}
