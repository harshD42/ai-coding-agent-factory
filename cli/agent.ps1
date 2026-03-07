# agent.ps1 — CLI for the AI Coding Agent Orchestrator
# Usage: .\cli\agent.ps1 <command> [args]
#
# Commands:
#   architect "<task>"     Spawn architect agent
#   debate    "<topic>"    Run architect vs reviewer debate
#   review    "<text>"     Spawn reviewer agent
#   test      "<task>"     Spawn tester agent
#   execute   [session]    Execute task queue for a session
#   status                 Show system status
#   list                   List all agents
#   memory    "<query>"    Search past sessions
#   index                  Re-index the codebase
#   learn     "<topic>"    Extract skill from session
#   logs      <agent_id>   Get logs for a specific agent

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
        Write-Host "Executed: $($r.executed)  Complete: $($r.complete)  Failed: $($r.failed)"
    }
    "status" {
        $r = Invoke-Orch GET "/v1/agents/status"
        Write-Host "Agents — Total: $($r.total)  Running: $($r.running)  Done: $($r.done)  Failed: $($r.failed)"
        $p = Invoke-Orch GET "/v1/patches/status"
        Write-Host "Patches — Total: $($p.total)  Pending: $($p.pending)  Applied: $($p.applied)  Rejected: $($p.rejected)"
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
    "index" {
        $r = Invoke-Orch POST "/v1/index"
        Write-Host "Indexed: $($r.files_indexed) files, $($r.chunks) chunks, $($r.skipped) skipped"
    }
    "learn" {
        $sid = if ($Args1) { $Args1 } else { $Session }
        $r = Invoke-Orch POST "/v1/skills/learn" @{session_id=$sid; transcript=@()}
        Write-Host "Skill extracted: $($r.skill_extracted)  Name: $($r.skill_name)"
    }
    "logs" {
        if (!$Args1) { Write-Error "Usage: agent.ps1 logs <agent_id>"; exit 1 }
        $r = Invoke-Orch GET "/v1/agents/$Args1/logs"
        $r | Format-List
    }
    default {
        Write-Host "AI Coding Agent Factory CLI"
        Write-Host ""
        Write-Host "Usage: .\cli\agent.ps1 <command> [args]"
        Write-Host ""
        Write-Host "Commands:"
        Write-Host "  architect '<task>'    Spawn architect agent"
        Write-Host "  debate    '<topic>'   Run architect vs reviewer debate"
        Write-Host "  review    '<text>'    Spawn reviewer agent"
        Write-Host "  test      '<task>'    Spawn tester agent"
        Write-Host "  execute   [session]   Execute task queue"
        Write-Host "  status                Show system status"
        Write-Host "  list                  List all agents"
        Write-Host "  memory    '<query>'   Search past sessions"
        Write-Host "  index                 Re-index the codebase"
        Write-Host "  learn     [session]   Extract skill from session"
        Write-Host "  logs      <agent_id>  Get agent logs"
    }
}