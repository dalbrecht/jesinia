// SPDX-License-Identifier: AGPL-3.0-or-later
// The metrics trend (plan Task 15, spec §5.2): a repo's scores kept over time, drawn under the
// Metrics page so a drift in quality shows.
import type { components } from "@jesinia/client";
import { fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { TrendChart } from "../pr/Trend";
import { queue, routes } from "./fixtures";
import { API, ARTIFACTS, drafts, PATH, reviewed } from "./pr-fixtures";

type Point = components["schemas"]["PointOut"];
const METRICS = "/api/review/metrics";

const point = (round: number, number: number, mean: number, criterion = "Error handling"): Point => ({
  round_id: round,
  number,
  head_sha: "a".repeat(40),
  at: `2026-10-0${round}T12:00:00Z`,
  mean,
  scores: [{ criterion, score: mean }],
});

function setup(over: Routes = {}, view = reviewed()) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const openMetrics = async () => {
  await screen.findByRole("heading", { level: 2, name: "Overview" });
  await userEvent.click(within(outline()).getByRole("link", { name: /Metrics/ }));
};
const trend = async () => screen.findByRole("figure", { name: /Quality trend/ });
/** Each drawn point's tooltip (an SVG `<title>` in its circle), in drawing order. */
const pointTitles = (figure: HTMLElement) =>
  [...figure.querySelectorAll("circle > title")].map((title) => title.textContent);

beforeEach(() => localStorage.clear());

describe("the trend under Metrics", () => {
  it("asks for the PR's repo and draws one point per scored review", async () => {
    const { calls } = setup({
      [`GET ${METRICS}`]: () =>
        Response.json({ repo: "acme/web", points: [point(1, 1, 6), point(2, 2, 7.5), point(3, 3, 9)] }),
    });
    await openMetrics();
    const figure = await trend();
    await waitFor(() => expect(pointTitles(figure)).toHaveLength(3));
    const asked = calls.filter((c) => new URL(c.url).pathname === METRICS);
    expect(asked.map((c) => new URL(c.url).searchParams.get("repo"))).toEqual(["acme/web"]);
    expect(pointTitles(figure)).toEqual([
      "#1 · 6 of 10 · Oct 1, 2026",
      "#2 · 7.5 of 10 · Oct 2, 2026",
      "#3 · 9 of 10 · Oct 3, 2026",
    ]);
    const chart = within(figure).getByRole("img");
    expect(chart).toHaveAccessibleName(
      "Mean score per review of acme/web, from 0 to 10: 3 reviews, from 6 (#1) to 9 (#3); lowest 6, highest 9.",
    );
    // The numbers are there for reading too, newest first.
    const rows = within(figure).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "ReviewWhenMeanScores",
      "#3Oct 3, 20269Error handling: 9",
      "#2Oct 2, 20267.5Error handling: 7.5",
      "#1Oct 1, 20266Error handling: 6",
    ]);
  });

  it("says so when the repo has no scored review yet, or the trend can't be read", async () => {
    setup({ [`GET ${METRICS}`]: () => Response.json({ repo: "acme/web", points: [] }) });
    await openMetrics();
    expect(await within(await trend()).findByText("No scored reviews of acme/web yet.")).toBeInTheDocument();
  });

  it("shows why the trend couldn't load", async () => {
    setup({ [`GET ${METRICS}`]: () => Response.json({ error: "X", detail: "the cache is busy" }, { status: 500 }) });
    await openMetrics();
    expect(await within(await trend()).findByText("Couldn't load the trend: the cache is busy.")).toBeInTheDocument();
  });
});

describe("the trend without this PR's own metrics (M7)", () => {
  it("still shows the repo's trend while the metrics job is pending, with a way to edit the set", async () => {
    const { metrics, ...rest } = ARTIFACTS;
    void metrics;
    const jobs = { ...ARTIFACTS.jobs, metrics: { state: "running", error: null } };
    setup(
      { [`GET ${METRICS}`]: () => Response.json({ repo: "acme/web", points: [point(1, 1, 6)] }) },
      reviewed({ artifacts: { ...rest, jobs } }),
    );
    await screen.findByRole("heading", { level: 2, name: "Overview" });
    await userEvent.click(within(outline()).getByRole("link", { name: /Metrics/ }));
    const page = screen.getByRole("article", { name: "Metrics" });
    expect(within(page).getByText("Metrics is being generated: it shows here when it finishes.")).toBeInTheDocument();
    const figure = await within(page).findByRole("figure", { name: "Quality trend for acme/web" });
    await waitFor(() => expect(pointTitles(figure)).toHaveLength(1));
    expect(within(figure).getByRole("link", { name: "Edit this repo's metrics" })).toHaveAttribute(
      "href",
      "/settings/#review",
    );
  });
});

describe("TrendChart", () => {
  it("lists the latest 20 reviews and shows the rest on request, in a named table (M8)", async () => {
    const many = Array.from({ length: 25 }, (_, i) => ({ ...point(1, i + 1, i % 11), round_id: i + 1 }));
    render(<TrendChart repo="acme/web" points={many} />);
    const table = screen.getByRole("table", { name: "Scores per review of acme/web, newest first" });
    expect(within(table).getAllByRole("row")).toHaveLength(21); // the header and 20 reviews
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent(/^#25/);
    expect(pointTitles(screen.getByRole("figure"))).toHaveLength(25); // the chart draws them all
    await userEvent.click(screen.getByRole("button", { name: "Show all 25 reviews" }));
    expect(within(table).getAllByRole("row")).toHaveLength(26);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("draws a single review as one point and no line", () => {
    render(<TrendChart repo="acme/web" points={[point(1, 12, 7.5)]} />);
    const figure = screen.getByRole("figure", { name: "Quality trend for acme/web" });
    expect(pointTitles(figure)).toEqual(["#12 · 7.5 of 10 · Oct 1, 2026"]);
    expect(figure.querySelector("polyline")).toBeNull();
    expect(within(figure).getByText("One scored review so far: 7.5 of 10.")).toBeInTheDocument();
  });

  it("joins two or more reviews with a line in order", () => {
    render(<TrendChart repo="acme/web" points={[point(1, 1, 0), point(2, 2, 10)]} />);
    const line = screen.getByRole("figure").querySelector("polyline");
    expect(line?.getAttribute("points")?.split(" ")).toHaveLength(2);
  });

  it("shows an agent's hidden characters in a criterion as visible tokens", () => {
    render(<TrendChart repo="acme/web" points={[point(1, 1, 5, "Tests\u202E<b>x</b>")]} />);
    const figure = screen.getByRole("figure");
    expect(within(figure).getByText("⟪U+202E⟫")).toBeInTheDocument();
    expect(figure.querySelector("b")).toBeNull();
  });
});
