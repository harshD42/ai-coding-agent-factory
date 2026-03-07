# agent.ps1 — CLI for the AI Coding Agent Orchestrator
# Usage: .\cli\agent.ps1 <command> [args]
#
# Commands:
#   architect  "<task>"      Spawn architect agent
#   debate     "<topic>"     Run architect vs reviewer debate
#   review     "<text>"      Spawn reviewer agent
#   test       "<task>"      Spawn tester agent
#   execute    [session]     Execute task queue for a session
#   status                   Show system status (agents + patches + metrics + training)
#   list                     List all agents
#   memory     "<query>"     Search past sessions
#   symbol     "<name>"      Search codebase by function/class name (Phase 3.1)
#   index                    Re-index the codebase (AST-aware)
#   learn      [session]     Extract skill from session
#   metrics    [session]     Show token counts and latency metrics (Phase 2.3)
#   finetune                 Show fine-tune training data stats (Phase 3.2)
#   export     [limit]       Export fine-tune training data to training_data.jsonl (Phase 3.2)
#   logs       <agent_id>    Get logs for a specific agent

param(
    [Parameter(Position=0)] [string]$Command,
    [Parameter(Position=1)] [string]$Args1 = "",
    [Parameter(Position=2)] [string]$Session = "cli-session"
)

$ORCH = $env:ORCH_URL ?? "http://localhost:9000"

function Invoke-Orch($method, $path, $body=$null) {
    $uri = "$ORCH$path"
    try {
        if ($body) {
            $json = $body | ConvertTo-Json -Depth 10
            $r = Invoke-RestMethod -Uri $uri -Method $method `
                 -ContentType "application/json" -Body $json
        } else {
            $r = Invoke-RestMethod -Uri $uri -Method $method
        }
        return $r
    } catch {
        $msg = $_.ErrorDetails.Message ?? $_.Exception.Message
        Write-Error "Request failed: $msg"
        exit 1
    }
}

switch ($Command.ToLower()) {
    "architect" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 architect '<task>'"; exit 1 }
        $r = Invoke-Orch POST "/v1/agents/spawn" @{role="architect"; task=$Args1; session_id=$Session}
        Write-Host "Agent: $($r.agent_id)  Status: $($r.status)"
        Write-Host ""
        Write-Host $r.result
    }
    "debate" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 debate '<topic>'"; exit 1 }
        $r = Invoke-Orch POST "/v1/agents/debate" @{topic=$Args1; session_id=$Session}
        Write-Host "Rounds: $($r.rounds)  Consensus: $($r.consensus)"
        Write-Host ""
        Write-Host $r.final_plan
    }
    "review" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 review '<text>'"; exit 1 }
        $r = Invoke-Orch POST "/v1/agents/spawn" @{role="reviewer"; task=$Args1; session_id=$Session}
        Write-Host $r.result
    }
    "test" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 test '<task>'"; exit 1 }
        $r = Invoke-Orch POST "/v1/agents/spawn" @{role="tester"; task=$Args1; session_id=$Session}
        Write-Host $r.result
    }
    "execute" {
        $sid = if ($Args1) { $Args1 } else { $Session }
        $r = Invoke-Orch POST "/v1/tasks/execute" @{session_id=$sid}
        Write-Host "Executed : $($r.executed)"
        Write-Host "Complete : $($r.complete)"
        Write-Host "Failed   : $($r.failed)"
        Write-Host "Blocked  : $($r.blocked)"
        if ($r.tasks) {
            Write-Host ""
            $r.tasks | ForEach-Object {
                $patches = if ($_.patches_applied -ne $null) { "  patches=$($_.patches_applied)" } else { "" }
                Write-Host "  [$($_.status)] $($_.id) ($($_.role))$patches"
            }
        }
    }
    "status" {
        $r  = Invoke-Orch GET "/v1/agents/status"
        $p  = Invoke-Orch GET "/v1/patches/status"
        $m  = Invoke-Orch GET "/v1/metrics"
        $ft = Invoke-Orch GET "/v1/finetune/stats"
        Write-Host "=== System Status ==="
        Write-Host "Agents   — Total: $($r.total)  Running: $($r.running)  Done: $($r.done)  Failed: $($r.failed)"
        Write-Host "Patches  — Total: $($p.total)  Pending: $($p.pending)  Applied: $($p.applied)  Rejected: $($p.rejected)"
        Write-Host "Metrics  — Requests: $($m.total_requests)  Tokens in: $($m.total_tokens_in)  Tokens out: $($m.total_tokens_out)  Avg latency: $($m.avg_latency_ms)ms"
        Write-Host "Training — $($ft.records) examples collected"
    }
    "list" {
        $r = Invoke-Orch GET "/v1/agents/list"
        $r.agents | Format-Table agent_id, role, status, task -AutoSize
    }
    "memory" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 memory '<query>'"; exit 1 }
        $r = Invoke-Orch GET "/v1/memory/recall?q=$([uri]::EscapeDataString($Args1))"
        if ($r.results.Count -eq 0) {
            Write-Host "No results found."
        } else {
            $r.results | ForEach-Object {
                Write-Host "[$($_.collection)] dist=$([math]::Round($_.distance,2))"
                Write-Host $_.content.Substring(0, [math]::Min(200, $_.content.Length))
                Write-Host "---"
            }
        }
    }
    "symbol" {
        # Phase 3.1 — search codebase by function/class name
        if (!$Args1) { Write-Error "Usage: agent.ps1 symbol '<name>'"; exit 1 }
        $r = Invoke-Orch GET "/v1/memory/symbol?name=$([uri]::EscapeDataString($Args1))"
        if ($r.count -eq 0) {
            Write-Host "No symbols found matching '$Args1'."
        } else {
            Write-Host "Found $($r.count) result(s) for '$($r.query)':"
            Write-Host ""
            $r.results | ForEach-Object {
                $m = $_.metadata
                Write-Host "  $($m.file)  [$($m.symbol_type)] $($m.symbol)  lines $($m.start_line)-$($m.end_line)"
                Write-Host "  $($_.content.Substring(0, [math]::Min(120, $_.content.Length)).Trim())"
                Write-Host "  ---"
            }
        }
    }
    "index" {
        $r = Invoke-Orch POST "/v1/index"
        Write-Host "Indexed : $($r.files_indexed) files"
        Write-Host "Chunks  : $($r.chunks)"
        Write-Host "Skipped : $($r.skipped)"
    }
    "learn" {
        $sid = if ($Args1) { $Args1 } else { $Session }
        $r = Invoke-Orch POST "/v1/skills/learn" @{session_id=$sid; transcript=@()}
        Write-Host "Skill extracted : $($r.skill_extracted)"
        if ($r.skill_name) { Write-Host "Skill name      : $($r.skill_name)" }
    }
    "metrics" {
        # Phase 2.3 — token counts and latency
        $path = if ($Args1) { "/v1/metrics?session_id=$([uri]::EscapeDataString($Args1))" } else { "/v1/metrics" }
        $r = Invoke-Orch GET $path
        Write-Host "=== Metrics ==="
        if ($r.session_id) {
            Write-Host "Session     : $($r.session_id)"
            Write-Host "Requests    : $($r.requests)"
            Write-Host "Tokens in   : $($r.tokens_in)"
            Write-Host "Tokens out  : $($r.tokens_out)"
            Write-Host "Avg latency : $($r.avg_latency_ms)ms"
        } else {
            Write-Host "Total requests  : $($r.total_requests)"
            Write-Host "Total tokens in : $($r.total_tokens_in)"
            Write-Host "Total tokens out: $($r.total_tokens_out)"
            Write-Host "Avg latency     : $($r.avg_latency_ms)ms"
            if ($r.by_role) {
                Write-Host ""
                Write-Host "By role:"
                $r.by_role.PSObject.Properties | ForEach-Object {
                    Write-Host "  $($_.Name): $($_.Value.requests) requests, avg $($_.Value.avg_latency_ms)ms"
                }
            }
        }
    }
    "finetune" {
        # Phase 3.2 — training data stats
        $r = Invoke-Orch GET "/v1/finetune/stats"
        Write-Host "Training examples : $($r.records)"
        Write-Host "File size         : $($r.size_bytes) bytes"
        Write-Host "Path              : $($r.path)"
    }
    "export" {
        # Phase 3.2 — export training data
        $limit = if ($Args1) { "?limit=$Args1" } else { "" }
        $uri   = "$ORCH/v1/finetune/export$limit"
        $out   = "training_data.jsonl"
        try {
            Invoke-WebRequest -Uri $uri -OutFile $out
            Write-Host "Exported to $out"
        } catch {
            Write-Error "Export failed: $($_.Exception.Message)"
        }
    }
    "logs" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 logs <agent_id>"; exit 1 }
        $r = Invoke-Orch GET "/v1/agents/$Args1/logs"
        $r | Format-List
    }
    default {
        Write-Host "AI Coding Agent Factory CLI  (v0.3.0)"
        Write-Host ""
        Write-Host "Usage: .\cli\agent.ps1 <command> [args]"
        Write-Host ""
        Write-Host "Core commands:"
        Write-Host "  architect  '<task>'    Spawn architect agent"
        Write-Host "  debate     '<topic>'   Run architect vs reviewer debate"
        Write-Host "  review     '<text>'    Spawn reviewer agent"
        Write-Host "  test       '<task>'    Spawn tester agent"
        Write-Host "  execute    [session]   Execute task queue (parallel)"
        Write-Host "  status                 System health + metrics + training"
        Write-Host "  list                   List all agents"
        Write-Host ""
        Write-Host "Memory commands:"
        Write-Host "  memory     '<query>'   Search past sessions"
        Write-Host "  symbol     '<name>'    Search codebase by function/class name"
        Write-Host "  index                  Re-index codebase (AST-aware)"
        Write-Host "  learn      [session]   Extract skill from session"
        Write-Host ""
        Write-Host "Observability commands:"
        Write-Host "  metrics    [session]   Token counts and latency"
        Write-Host "  finetune               Training data stats"
        Write-Host "  export     [limit]     Export training data to JSONL"
        Write-Host ""
        Write-Host "Debug commands:"
        Write-Host "  logs       <agent_id>  Get agent logs"
        Write-Host ""
        Write-Host "Environment:"
        Write-Host "  ORCH_URL   Override orchestrator URL (default: http://localhost:9000)"
    }
}