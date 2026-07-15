/**
 * Router regression tests for removed/deprecated top-level routes.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Route } from "wouter";

import { Router } from "@/App";

vi.mock("@/lib/protected-route", () => ({
  ProtectedRoute: ({
    path,
    component: Component,
  }: {
    path: string;
    component: () => React.JSX.Element;
  }) => <Route path={path} component={Component} />,
}));

vi.mock("@/pages/dashboard", () => ({ default: () => <div>dashboard-page</div> }));
vi.mock("@/pages/knowledge-workspace-page", () => ({ default: () => <div>knowledge-workspace-page</div> }));
vi.mock("@/pages/reports-page", () => ({ default: () => <div>reports-page</div> }));
vi.mock("@/pages/settings-page", () => ({ default: () => <div>settings-page</div> }));
vi.mock("@/pages/profile-page", () => ({ default: () => <div>profile-page</div> }));
vi.mock("@/pages/auth-page", () => ({ default: () => <div>auth-page</div> }));
vi.mock("@/pages/setup", () => ({ default: () => <div>setup-page</div> }));
vi.mock("@/pages/not-found", () => ({ default: () => <div>not-found-page</div> }));

describe("app router", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState({}, "", "/");
  });

  it("routes /agent to not-found after Live Agent removal", () => {
    window.history.pushState({}, "", "/agent");

    render(<Router />);

    expect(screen.getByText("not-found-page")).toBeTruthy();
  });

  it("routes /tasks to not-found after standalone Tasks page removal", () => {
    window.history.pushState({}, "", "/tasks");

    render(<Router />);

    expect(screen.getByText("not-found-page")).toBeTruthy();
  });

  it("routes task memory flow URLs to not-found after Memory Flow removal", () => {
    window.history.pushState({}, "", "/tasks/42/memory-flow");

    render(<Router />);

    expect(screen.getByText("not-found-page")).toBeTruthy();
  });
});
