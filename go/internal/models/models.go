// Package models defines the JSON models for the Loop Controller A2A kernel.
// These models mirror proto/loop_controller/a2a/v1/a2a.proto.
package models

import (
	"encoding/json"
	"time"
)

// AgentCard describes an agent that can participate in governed interactions.
type AgentCard struct {
	AgentID      string          `json:"agent_id" yaml:"agent_id"`
	Name         string          `json:"name" yaml:"name"`
	Description  string          `json:"description" yaml:"description"`
	Entrypoint   AgentEntrypoint `json:"entrypoint" yaml:"entrypoint"`
	Capabilities []string        `json:"capabilities" yaml:"capabilities"`
	TrustDomain  string          `json:"trust_domain" yaml:"trust_domain"`
	Version      string          `json:"version" yaml:"version"`
}

// AgentEntrypoint describes how to reach an agent.
type AgentEntrypoint struct {
	Type string `json:"type" yaml:"type"`
	URL  string `json:"url" yaml:"url"`
}

// Task represents an interaction context between two agents.
type Task struct {
	TaskID           string    `json:"task_id"`
	SessionID        string    `json:"session_id"`
	InitiatorAgentID string    `json:"initiator_agent_id"`
	TargetAgentID    string    `json:"target_agent_id"`
	Status           string    `json:"status"`
	CreatedAt        time.Time `json:"created_at"`
	UpdatedAt        time.Time `json:"updated_at"`
}

// Message is a unit of communication between agents.
type Message struct {
	MessageID       string    `json:"message_id"`
	TaskID          string    `json:"task_id"`
	FromAgentID     string    `json:"from_agent_id"`
	ToAgentID       string    `json:"to_agent_id"`
	Role            string    `json:"role"`
	Parts           []Part    `json:"parts"`
	Timestamp       time.Time `json:"timestamp"`
	ProtocolVersion string    `json:"protocol_version,omitempty"`
}

// Part is a fragment of a message.
type Part struct {
	Type string          `json:"type"`
	Text string          `json:"text,omitempty"`
	Data json.RawMessage `json:"data,omitempty"`
}

// DelegationRequest is sent by the Python tool-governance layer when it wants
// to forward a tool execution to another agent.
type DelegationRequest struct {
	RequestID        string `json:"request_id"`
	InitiatorAgentID string `json:"initiator_agent_id"`
	TargetAgentID    string `json:"target_agent_id"`
	ToolName         string `json:"tool_name"`
	ArgumentsJSON    string `json:"arguments_json"`
	SessionID        string `json:"session_id"`
	TaskID           string `json:"task_id"`
	RiskLevel        string `json:"risk_level"`
	ProtocolVersion  string `json:"protocol_version,omitempty"`
}

// DelegationResponse is returned by the Go kernel.
type DelegationResponse struct {
	Allowed           bool            `json:"allowed"`
	TaskID            string          `json:"task_id"`
	TargetEntrypoint  AgentEntrypoint `json:"target_entrypoint,omitempty"`
	DelegationToken   string          `json:"delegation_token,omitempty"`
	Reason            string          `json:"reason"`
	ProtocolVersion   string          `json:"protocol_version,omitempty"`
}

// SendMessageResponse indicates whether a message was accepted for routing.
type SendMessageResponse struct {
	Accepted        bool   `json:"accepted"`
	Reason          string `json:"reason"`
	ProtocolVersion string `json:"protocol_version,omitempty"`
}

// AgentList is the response for listing registered agents.
type AgentList struct {
	Agents []AgentCard `json:"agents"`
}

// ErrorResponse is the standard error envelope.
type ErrorResponse struct {
	Error   string `json:"error"`
	Code    string `json:"code"`
}
