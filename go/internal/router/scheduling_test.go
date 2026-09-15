package router

import (
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestRankFilteringOrderAndDeterminism(t *testing.T) {
	now := time.Now().UTC()
	req := models.SchedulingRequest{RequestID: "r", TenantID: "t", TaskID: "task", TrustDomain: "td", RequiredAgentCapabilities: []string{"cap"}, RequiredSecurityCapabilities: []string{"sec"}, RequiredTools: []string{"tool"}}
	spec := models.AgentSpec{TenantID: "wrong", AgentID: "b", TrustDomain: "wrong", SupportedProtocolVersions: []string{"x"}, ExpectedWorkloadID: "wrong", MaxConcurrency: 1, Generation: 2}
	status := &models.AgentStatus{ObservedGeneration: 1, Health: models.AgentHealthUnhealthy, Draining: true, ExpiresAt: now.Add(-time.Second), AvailableCapacity: 0}
	in := SchedulingInput{Request: req, ProtocolVersion: "0.53.0", WorkloadID: "work", CapacityUnits: 1, BudgetAvailable: true, Now: now}
	first, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: spec, Status: status}}})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{models.SchedulingReasonTenantMismatch, models.SchedulingReasonTrustDomainMismatch, ReasonProtocolMismatch, models.SchedulingReasonWorkloadMismatch, models.SchedulingReasonSecurityMismatch, models.SchedulingReasonCapabilityMismatch, models.SchedulingReasonToolMismatch, ReasonObservedGenerationMismatch, models.SchedulingReasonStatusExpired, models.SchedulingReasonUnhealthy, models.SchedulingReasonDraining, models.SchedulingReasonNotSchedulable}
	if !reflect.DeepEqual(first.Candidates[0].ReasonCodes, want) {
		t.Fatalf("reasons=%v want=%v", first.Candidates[0].ReasonCodes, want)
	}
	second, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: healthySpec("z"), Status: healthyStatus(now)}}, {Record: models.AgentRecord{Spec: healthySpec("a"), Status: healthyStatus(now)}}})
	if err != nil {
		t.Fatal(err)
	}
	reversed, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: healthySpec("a"), Status: healthyStatus(now)}}, {Record: models.AgentRecord{Spec: healthySpec("z"), Status: healthyStatus(now)}}})
	if err != nil {
		t.Fatal(err)
	}
	if second.SelectedAgentID != "a" || !reflect.DeepEqual(second, reversed) {
		t.Fatalf("non deterministic: %+v %+v", second, reversed)
	}
}

func TestRankUsesIntegerLoadRatioAndTieBreaks(t *testing.T) {
	now := time.Now().UTC()
	req := models.SchedulingRequest{RequestID: "r", TenantID: "t", TaskID: "task", TrustDomain: "td"}
	in := SchedulingInput{Request: req, CapacityUnits: 1, BudgetAvailable: true, Now: now}
	a, b := healthySpec("a"), healthySpec("b")
	a.MaxConcurrency = 3
	b.MaxConcurrency = 2
	sa, sb := healthyStatus(now), healthyStatus(now)
	sa.CurrentLoad = 1
	sb.CurrentLoad = 1
	d, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: b, Status: sb}}, {Record: models.AgentRecord{Spec: a, Status: sa}}})
	if err != nil {
		t.Fatal(err)
	}
	if d.SelectedAgentID != "a" {
		t.Fatalf("selected=%s", d.SelectedAgentID)
	}
}

func TestRankRejectsDegradedAndIgnoresReportedCapacityForAdmission(t *testing.T) {
	now := time.Now().UTC()
	in := SchedulingInput{Request: models.SchedulingRequest{RequestID: "r", TenantID: "t", TaskID: "task", TrustDomain: "td"}, CapacityUnits: 1, BudgetAvailable: true, Now: now}
	degradedSpec, admittedSpec := healthySpec("degraded"), healthySpec("admitted")
	degraded := healthyStatus(now)
	degraded.Health = models.AgentHealthDegraded
	admitted := healthyStatus(now)
	admitted.AvailableCapacity = 0
	admitted.CurrentLoad = admittedSpec.MaxConcurrency

	d, err := Rank(in, []SchedulingAgent{
		{Record: models.AgentRecord{Spec: degradedSpec, Status: degraded}},
		{Record: models.AgentRecord{Spec: admittedSpec, Status: admitted}, ReservedUnits: admittedSpec.MaxConcurrency - 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	if d.SelectedAgentID != "admitted" {
		t.Fatalf("selected=%q candidates=%+v", d.SelectedAgentID, d.Candidates)
	}
	if !reflect.DeepEqual(d.Candidates[1].ReasonCodes, []string{models.SchedulingReasonUnhealthy}) {
		t.Fatalf("degraded reasons=%v", d.Candidates[1].ReasonCodes)
	}
}

func TestRankLargeLoadRatioDoesNotOverflow(t *testing.T) {
	now := time.Now().UTC()
	maxInt := int(^uint(0) >> 1)
	in := SchedulingInput{Request: models.SchedulingRequest{RequestID: "r", TenantID: "t", TaskID: "task", TrustDomain: "td"}, CapacityUnits: 1, BudgetAvailable: true, Now: now}
	a, b := healthySpec("a"), healthySpec("b")
	a.MaxConcurrency, b.MaxConcurrency = maxInt, maxInt
	sa, sb := healthyStatus(now), healthyStatus(now)
	sa.CurrentLoad, sb.CurrentLoad = maxInt, maxInt-1

	first, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: a, Status: sa}}, {Record: models.AgentRecord{Spec: b, Status: sb}}})
	if err != nil {
		t.Fatal(err)
	}
	reversed, err := Rank(in, []SchedulingAgent{{Record: models.AgentRecord{Spec: b, Status: sb}}, {Record: models.AgentRecord{Spec: a, Status: sa}}})
	if err != nil {
		t.Fatal(err)
	}
	if first.SelectedAgentID != "b" || !reflect.DeepEqual(first, reversed) {
		t.Fatalf("large ratio ordering is not safe and deterministic: %+v %+v", first, reversed)
	}
}

func TestValidateRejectsEmptySchedulingSelectors(t *testing.T) {
	base := SchedulingInput{Request: models.SchedulingRequest{RequestID: "r", TenantID: "t", TaskID: "task"}, CapacityUnits: 1}
	tests := []models.SchedulingRequest{
		{RequestID: "r", TenantID: "t", TaskID: "task", RequiredAgentCapabilities: []string{""}},
		{RequestID: "r", TenantID: "t", TaskID: "task", RequiredSecurityCapabilities: []string{""}},
		{RequestID: "r", TenantID: "t", TaskID: "task", RequiredTools: []string{""}},
		{RequestID: "r", TenantID: "t", TaskID: "task", ExcludedAgentIDs: []string{""}},
		{RequestID: "r", TenantID: "t", TaskID: "task", Affinity: map[string]string{"": "value"}},
		{RequestID: "r", TenantID: "t", TaskID: "task", AntiAffinity: map[string]string{"": "value"}},
	}
	for _, request := range tests {
		base.Request = request
		if err := Validate(base); !errors.Is(err, ErrInvalidSchedulingRequest) {
			t.Fatalf("request=%+v err=%v", request, err)
		}
	}
}

func healthySpec(id string) models.AgentSpec {
	return models.AgentSpec{TenantID: "t", AgentID: id, TrustDomain: "td", Capabilities: []string{"cap"}, SupportedTools: []string{"tool"}, SupportedSecurityCapabilities: []string{"sec"}, SupportedProtocolVersions: []string{"0.53.0"}, ExpectedWorkloadID: "work", MaxConcurrency: 10, Schedulable: true, Generation: 1, ResourceVersion: 1}
}
func healthyStatus(now time.Time) *models.AgentStatus {
	return &models.AgentStatus{TenantID: "t", ObservedGeneration: 1, Health: models.AgentHealthHealthy, AvailableCapacity: 10, ExpiresAt: now.Add(time.Hour), StatusRevision: 1}
}
