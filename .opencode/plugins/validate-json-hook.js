/**
 * validate-json-hook — 写入前拦截校验 knowledge/articles/ 下的 JSON 文件
 *
 * 监听 tool.execute.before 事件，在 write 工具实际执行前，
 * 从 output.args.content 取出内容，通过 stdin 传给
 * hooks/validate_json.py --stdin 校验。
 *
 * 校验失败时抛出异常，阻止写入操作执行，agent 会收到
 * 明确的错误信息并自行修复。
 */

import { appendFileSync } from "node:fs";
import { join } from "node:path";

const ARTICLES_DIR = "knowledge/articles";
const LOG_FILE = join(import.meta.dir, "plugin.log");

function fileLog(msg) {
  const ts = new Date().toISOString();
  appendFileSync(LOG_FILE, `[${ts}] ${msg}\n`);
}

fileLog("=== plugin loaded (before mode) ===");

function isArticlesJson(filePath) {
  if (!filePath) return false;
  const normalized = filePath.replace(/\\/g, "/");
  return normalized.includes(ARTICLES_DIR) && normalized.endsWith(".json");
}

export const ValidateJsonHook = async ({ $, directory }) => {
  fileLog(`init: directory=${directory}`);

  return {
    "tool.execute.before": async (input, output) => {
      const toolName = input.tool || "unknown";
      if (toolName !== "write") return;

      const filePath = output.args?.filePath;
      if (!isArticlesJson(filePath)) return;

      const content = output.args?.content;
      if (!content) return;

      fileLog(`before: tool=${toolName} filePath=${filePath}`);

      const result = await $`echo ${content} | python3 hooks/validate_json.py --stdin`.nothrow();
      const exitCode = result.exitCode;
      const stdout = result.stdout?.toString().trim() || "";
      fileLog(`validate: exitCode=${exitCode} stdout=${stdout}`);

      if (exitCode !== 0) {
        const stderr = result.stderr?.toString().trim() || stdout;
        fileLog(`BLOCKED: ${filePath} - ${stderr}`);
        throw new Error(
          `[validate-json-hook] 写入被拦截，请修复以下问题后重试:\n${stderr}\n\n文件: ${filePath}`
        );
      }

      fileLog(`PASS: ${filePath}`);
    },
  };
};
