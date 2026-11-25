use super::*;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

async fn write_line(writer: &mut tokio::process::ChildStdin, value: &Value) -> anyhow::Result<()> {
    let mut bytes = serde_json::to_vec(value)?;
    bytes.push(b'\n');
    writer.write_all(&bytes).await?;
    writer.flush().await?;
    Ok(())
}

pub(super) async fn attempt(
    state: &AppState, run: Option<&Ctx>, call: &Call, prompt: String,
    servers: HashMap<String, McpServerConfig>, allowed: Vec<String>,
) -> anyhow::Result<Attempt> {
    let mut specs = Vec::new();
    let mut handlers = HashMap::new();
    for (namespace, config) in servers {
        let McpServerConfig::Sdk(server) = config else { anyhow::bail!("Unsupported external tool configuration") };
        let listing = server.instance.handle_message(json!({"id":1,"method":"tools/list"})).await?;
        let tools = listing["result"]["tools"].as_array().context("Missing Splee tool list")?;
        for tool in tools {
            let name = tool["name"].as_str().context("Missing Splee tool name")?;
            let exposed = format!("mcp__{namespace}__{name}");
            if allowed.contains(&exposed) {
                specs.push(json!({"type":"function","name":exposed,"description":tool["description"],"inputSchema":tool["inputSchema"]}));
                handlers.insert(exposed, (server.instance.clone(), name.to_string()));
            }
        }
    }
    let driver = std::env::var("SPLEE_CODEX_DRIVER")?;
    let mut child = Command::new("/usr/bin/python3").arg("-I").arg(driver)
        .env_clear().env("HOME",std::env::var("HOME")?).env("PATH","/usr/bin:/bin")
        .env("SPLEE_CODEX_BIN",std::env::var("SPLEE_CODEX_BIN")?)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
        .kill_on_drop(true).spawn().context("start Splee Codex driver")?;
    let mut input = child.stdin.take().context("Missing driver input")?;
    let mut lines = BufReader::new(child.stdout.take().context("Missing driver output")?).lines();
    let payload = json!({"tools":specs,"prompt":prompt,"schema":call.schema,"system":call.system,
        "web":call.web,"model":state.cfg.agent_model,"max_turns":call.max_turns});
    // Subscription usage has no per-call invoice. Reserve the configured allowance, even on cancellation.
    if let Some(ctx) = run { ctx.add_cost(call.budget_usd).await; }
    write_line(&mut input, &payload).await?;
    while let Some(line) = lines.next_line().await? {
        let msg: Value = serde_json::from_str(&line)?;
        let allowance = if run.is_some() { 0.0 } else { call.budget_usd };
        if let Some(value) = msg.get("answer") {
            return Ok(Attempt {output:Some(value.clone()), cost:allowance, error:None});
        }
        if msg.get("invalid").is_some() {
            return Ok(Attempt {output:None, cost:allowance, error:Some("Invalid structured output".into())});
        }
        if let Some(problem) = msg["failed"].as_str() { anyhow::bail!("Codex: {problem}"); }
        let name = msg["tool"].as_str().context("Missing tool name")?;
        let (server, tool) = handlers.get(name).context("Tool outside this task's scope")?;
        if let Some(ctx) = run { report_tool(state,ctx,name,msg["args"].clone()).await; }
        let result = server.handle_message(json!({"id":1,"method":"tools/call",
            "params":{"name":tool,"arguments":msg["args"]}})).await;
        let (error, text) = match result {
            Ok(response) if response.get("result").is_some() =>
                (response["result"]["isError"].as_bool().unwrap_or(false), response["result"]["content"].to_string()),
            _ => (true, String::from("Splee tool failed")),
        };
        write_line(&mut input,&json!({"error":error,"text":text})).await?;
    }
    anyhow::bail!("Codex driver exited before completing the task")
}
