/**
 * Verifies the Network tab is a live, read-only topology overview.
 */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NetworkSettingsPanel } from "@/components/settings/network-settings-panel";

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: ({ queryKey }) => {
          if (String(queryKey[0]) !== "/api/settings/network/overview") {
            throw new Error(`Unexpected endpoint: ${String(queryKey[0])}`);
          }
          return Promise.resolve({
            deployment_profile: "distributed",
            management: {
              advertised_url: "https://management.example.test",
              advertised_host: "management.example.test",
              advertised_url_source: "generated_config",
              primary_ip: "10.10.0.4",
              interfaces: [
                {
                  interface_name: "eth0",
                  address: "10.10.0.4",
                  family: "ipv4",
                  prefix_length: 24,
                  is_loopback: false,
                },
              ],
              gateway_ip: "10.10.0.1",
              gateway_interface: "eth0",
              dns_servers: ["10.10.0.53", "1.1.1.1"],
            },
            runners: [
              {
                id: "1c4bed00-65ed-493f-8e51-11c1119847de",
                name: "runner-istanbul",
                site_id: "5655118f-14cd-4fb6-857f-198521183d07",
                site_name: "Istanbul Site",
                site_network_label: "corp-edge",
                status: "online",
                connection_status: "connected",
                observed_ip: "198.51.100.11",
                observed_at: "2026-07-10T12:00:00Z",
              },
            ],
            collected_at: "2026-07-10T12:00:00Z",
          });
        },
      },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <NetworkSettingsPanel />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("<NetworkSettingsPanel />", () => {
  it("renders management routing and observed Runner connectivity", async () => {
    renderPanel();

    expect(await screen.findByText("Network overview")).toBeTruthy();
    expect(screen.getByText("Distributed")).toBeTruthy();
    expect(screen.getByText("10.10.0.4")).toBeTruthy();
    expect(screen.getByText("10.10.0.1")).toBeTruthy();
    expect(screen.getByText("10.10.0.53, 1.1.1.1")).toBeTruthy();
    expect(screen.getByText("runner-istanbul")).toBeTruthy();
    expect(screen.getByText("198.51.100.11")).toBeTruthy();
    expect(screen.getByText("Istanbul Site · corp-edge")).toBeTruthy();
  });

  it("contains no editable network controls", async () => {
    renderPanel();

    expect(await screen.findByText("Network overview")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });
});
