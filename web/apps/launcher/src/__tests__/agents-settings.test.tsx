// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Agents: each runner's model (setting, then environment variable, then the CLI's
// default), saved one field at a time with a fresh Idempotency-Key.
import type { components } from "@jesinia/client";
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";

type AgentSettings = components["schemas"]["AgentSettingsOut"];
type RunnerModel = components["schemas"]["RunnerModelOut"];

const model = (over: Partial<RunnerModel>): RunnerModel => ({
  setting: null,
  environment: null,
  variable: "JESINIA_CODEX_MODEL",
  effective: null,
  source: "default",
  ...over,
});

const SETTINGS: AgentSettings = {
  models: {
    "claude-code": model({
      environment: "claude-env",
      variable: "JESINIA_CLAUDE_MODEL",
      effective: "claude-env",
      source: "environment",
    }),
    codex: model({ setting: "gpt-a", effective: "gpt-a", source: "setting" }),
  },
};

/** The server's answer to a PATCH: each sent runner saved (null clears it). */
function patched(current: AgentSettings, body: Record<string, string | null>): AgentSettings {
  const models = { ...current.models };
  for (const [runner, value] of Object.entries(body)) {
    const was = models[runner] as RunnerModel;
    models[runner] =
      value === null
        ? {
            ...was,
            setting: null,
            effective: was.environment,
            source: was.environment ? "environment" : "default",
          }
        : { ...was, setting: value, effective: value, source: "setting" };
  }
  return { models };
}

function renderAgents(over: Routes = {}) {
  window.history.replaceState(null, "", "/settings/#agents");
  let current = SETTINGS;
  const env = fakeClient(
    baseRoutes({
      "GET /api/agents/settings": () => Response.json(current),
      "PATCH /api/agents/settings": async (req: Request) => {
        current = patched(current, (await req.clone().json()) as Record<string, string | null>);
        return Response.json(current);
      },
      ...over,
    }),
  );
  render(<App path="/settings/" client={env.client} />);
  return env.calls;
}

const patches = (calls: Request[]) =>
  calls.filter((c) => c.method === "PATCH" && new URL(c.url).pathname === "/api/agents/settings");
const lastPatch = async (calls: Request[]) => {
  const all = patches(calls);
  const req = all[all.length - 1] as Request;
  expect(req.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  return (await req.clone().json()) as Record<string, string | null>;
};
const field = async (name: string) => within(await screen.findByRole("group", { name: `${name} model` }));

afterEach(() => window.history.replaceState(null, "", "/settings/"));

describe("Settings → Agents models", () => {
  it("shows each runner's model, its default as the placeholder, and where it comes from", async () => {
    renderAgents();
    const claude = await field("Claude Code");
    const claudeInput = claude.getByLabelText("Model");
    expect(claudeInput).toHaveValue("");
    expect(claudeInput).toHaveAttribute("placeholder", "claude-env");
    expect(claude.getByText("From JESINIA_CLAUDE_MODEL.")).toBeInTheDocument();
    const codex = await field("Codex");
    expect(codex.getByLabelText("Model")).toHaveValue("gpt-a");
    expect(codex.getByText("Set here.")).toBeInTheDocument();
  });

  it("falls back to the CLI's default when nothing is set", async () => {
    renderAgents({
      "GET /api/agents/settings": { models: { codex: model({}) } },
    });
    const codex = await field("Codex");
    expect(codex.getByLabelText("Model")).toHaveAttribute("placeholder", "Codex default");
    expect(codex.getByText("Default: Codex picks its own model.")).toBeInTheDocument();
  });

  it("saves only the field that changed, and shows the new source", async () => {
    const calls = renderAgents();
    const claude = await field("Claude Code");
    const save = claude.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled(); // nothing changed yet
    await userEvent.type(claude.getByLabelText("Model"), "claude-opus-5-5");
    await userEvent.click(save);
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(await lastPatch(calls)).toEqual({ "claude-code": "claude-opus-5-5" });
    expect(await claude.findByText("Set here.")).toBeInTheDocument();
    expect((await screen.findAllByText("Claude Code model saved.")).length).toBeGreaterThan(0);
    // Codex was left alone: its value is untouched.
    expect((await field("Codex")).getByLabelText("Model")).toHaveValue("gpt-a");
  });

  it("clearing a field sends null, back to the environment or the default", async () => {
    const calls = renderAgents();
    const codex = await field("Codex");
    await userEvent.clear(codex.getByLabelText("Model"));
    await userEvent.click(codex.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(await lastPatch(calls)).toEqual({ codex: null });
    expect(await codex.findByText("Default: Codex picks its own model.")).toBeInTheDocument();
  });

  it("shows the server's refusal on the field", async () => {
    renderAgents({
      "PATCH /api/agents/settings": () =>
        Response.json({ detail: [{ msg: "String should match pattern" }] }, { status: 422 }),
    });
    const codex = await field("Codex");
    await userEvent.clear(codex.getByLabelText("Model"));
    await userEvent.type(codex.getByLabelText("Model"), "bad name");
    await userEvent.click(codex.getByRole("button", { name: "Save" }));
    expect(await codex.findByText("String should match pattern")).toBeInTheDocument();
  });

  it("says when the models couldn't load", async () => {
    renderAgents({
      "GET /api/agents/settings": () => Response.json({ detail: "agents module not started" }, { status: 503 }),
    });
    expect(await screen.findByText("Couldn't load the agent models: agents module not started")).toBeInTheDocument();
  });
});
