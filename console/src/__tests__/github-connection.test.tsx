import { act, fireEvent, render, screen, within } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  expect,
  it,
  vi,
  type MockInstance,
} from "vitest";
import { HttpSessionApi } from "../api/http";
import type { IntegrationStatus } from "../api/types";
import {
  GITHUB_PENDING_KEY,
  GITHUB_REQUEST_MS,
  GITHUB_RETURN_KEY,
  GITHUB_WAIT_MS,
} from "../api/github-return";
import { ApiProvider } from "../state/api";
import { Integrations } from "../prototype/Integrations";
import { connections } from "../prototype/domain";

type Status = IntegrationStatus["github"];
const disconnected: Status = {
  configured: false,
  installable: false,
  connected: false,
  accounts: [],
};
const connected: Status = {
  ...disconnected,
  configured: true,
  connected: true,
  brokerBound: true,
  accounts: ["soren-labs"],
};
const settle = async () => {
  await act(async () => {});
};
let client: HttpSessionApi;
let popup: {
  location: { href: string };
  opener: unknown;
  close: ReturnType<typeof vi.fn>;
};
let read: MockInstance<HttpSessionApi["getGithubStatus"]>;

beforeEach(() => {
  vi.useFakeTimers();
  sessionStorage.clear();
  history.replaceState(null, "", "/integrations");
  client = new HttpSessionApi();
  read = vi.spyOn(client, "getGithubStatus").mockResolvedValue(disconnected);
  vi.spyOn(client, "listProviders").mockResolvedValue([]);
  vi.spyOn(connections, "list").mockResolvedValue([]);
  vi.spyOn(connections, "syncGithub").mockResolvedValue();
  vi.spyOn(client, "beginGithubAuthorize").mockResolvedValue({
    url: "https://github.com/apps/sbx/installations/new",
  });
  popup = { location: { href: "about:blank" }, opener: {}, close: vi.fn() };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  sessionStorage.clear();
  history.replaceState(null, "", "/");
});
async function open() {
  const view = render(
    <ApiProvider client={client}>
      <Integrations />
    </ApiProvider>,
  );
  await settle();
  return view;
}
const card = () =>
  within(screen.getByRole("region", { name: "GitHub connection" }));

it("updates the actual Connections UI automatically when the popup callback binds an installation", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  await settle();
  expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
  expect(popup.opener).toBeNull();
  expect(popup.location.href).toContain(
    "github.com/apps/sbx/installations/new",
  );
  expect(card().getByText("Not connected")).toBeVisible();
  expect(sessionStorage.getItem(GITHUB_PENDING_KEY)).not.toBeNull();
  expect(card().getByRole("status")).toHaveTextContent(
    "confirm the connection automatically",
  );
  read.mockResolvedValue(connected);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(card().getByText("Connected")).toBeVisible();
  expect(card().getByText("soren-labs")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Sync repositories" }),
  ).toBeEnabled();
  expect(screen.getByText("GitHub installation connected.")).toBeVisible();
  expect(sessionStorage.getItem(GITHUB_PENDING_KEY)).toBeNull();
  const calls = read.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(read).toHaveBeenCalledTimes(calls);
});

it.each(["focus", "pageshow", "visibilitychange", "storage"])(
  "refetches backend truth on %s after returning to SBX",
  async (event) => {
    await open();
    read.mockResolvedValue(connected);
    await act(async () => {
      if (event === "storage")
        window.dispatchEvent(
          new StorageEvent("storage", {
            key: GITHUB_RETURN_KEY,
            newValue: JSON.stringify({ at: Date.now() }),
          }),
        );
      else if (event === "visibilitychange")
        document.dispatchEvent(new Event(event));
      else window.dispatchEvent(new Event(event));
    });
    expect(card().getByText("Connected")).toBeVisible();
  },
);

it("keeps a success return marker pending until the backend confirms it", async () => {
  history.replaceState(null, "", "/integrations?broker=connected");
  await open();
  expect(card().getByText("Not connected")).toBeVisible();
  expect(
    screen.queryByText("GitHub installation connected."),
  ).not.toBeInTheDocument();
  expect(location.search).toBe("");
  read.mockResolvedValue(connected);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(screen.getByText("GitHub installation connected.")).toBeVisible();
});

it("survives a same-tab handoff or reload with persisted waiting state", async () => {
  sessionStorage.setItem(
    GITHUB_PENDING_KEY,
    String(Date.now() + GITHUB_WAIT_MS),
  );
  await open();
  read.mockResolvedValue(connected);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(card().getByText("Connected")).toBeVisible();
});

it("shows callback errors even if an older installation remains connected", async () => {
  history.replaceState(
    null,
    "",
    "/integrations?broker_error=authorization_expired",
  );
  sessionStorage.setItem(
    GITHUB_PENDING_KEY,
    String(Date.now() + GITHUB_WAIT_MS),
  );
  read.mockResolvedValue(connected);
  await open();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "expired or was already used",
  );
  expect(
    screen.queryByText("GitHub installation connected."),
  ).not.toBeInTheDocument();
  expect(sessionStorage.getItem(GITHUB_PENDING_KEY)).toBeNull();
});

it("reports failed authorization to the original tab without faking success", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  await settle();
  await act(async () => {
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: GITHUB_RETURN_KEY,
        newValue: JSON.stringify({ error: "authorization_failed" }),
      }),
    );
  });
  expect(screen.getByRole("alert")).toHaveTextContent("did not complete");
  expect(card().getByText("Not connected")).toBeVisible();
  expect(sessionStorage.getItem(GITHUB_PENDING_KEY)).toBeNull();
});

it("bounds waiting and lets the user reconnect when authorization never completes", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  await settle();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(GITHUB_WAIT_MS);
  });
  expect(screen.getByRole("alert")).toHaveTextContent("has not been confirmed");
  expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeEnabled();
  const calls = read.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(7500);
  });
  expect(read).toHaveBeenCalledTimes(calls);
});

it("reports initial status failure and recovers through a real backend read", async () => {
  read.mockRejectedValueOnce(new Error("Connection unavailable"));
  await open();
  expect(screen.getByRole("alert")).toHaveTextContent("Connection unavailable");
  expect(card().getByText("Status unavailable")).toBeVisible();
  read.mockResolvedValue(connected);
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await settle();
  expect(card().getByText("Connected")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("closes the blank popup and preserves disconnected state when initiation fails", async () => {
  vi.spyOn(client, "beginGithubAuthorize").mockRejectedValue(
    new Error("GitHub broker unavailable"),
  );
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  await settle();
  expect(popup.close).toHaveBeenCalledOnce();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "GitHub broker unavailable",
  );
  expect(card().getByText("Not connected")).toBeVisible();
});

it("syncs repositories and drops a revoked installation from the UI", async () => {
  read.mockResolvedValueOnce(connected).mockResolvedValue(disconnected);
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Sync repositories" }));
  await settle();
  expect(connections.syncGithub).toHaveBeenCalledOnce();
  expect(card().getByText("Not connected")).toBeVisible();
  expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeEnabled();
});

it("keeps sync permission errors visible while refreshing the actual installation", async () => {
  read.mockResolvedValue(connected);
  vi.spyOn(connections, "syncGithub").mockRejectedValue(
    new Error("GitHub sync requires an administrator connection."),
  );
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Sync repositories" }));
  await settle();
  expect(screen.getByRole("alert")).toHaveTextContent("administrator");
  expect(card().getByText("Connected")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Sync repositories" }),
  ).toBeEnabled();
});

it("coalesces return events during a slow status read and never overlaps polling", async () => {
  let resolve!: (s: Status) => void;
  read.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  sessionStorage.setItem(
    GITHUB_PENDING_KEY,
    String(Date.now() + GITHUB_WAIT_MS),
  );
  await open();
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(read).toHaveBeenCalledOnce();
  read.mockResolvedValue(connected);
  await act(async () => {
    resolve(disconnected);
  });
  expect(read).toHaveBeenCalledTimes(2);
  expect(card().getByText("Connected")).toBeVisible();
});

it("ignores an old response after sync and removes listeners and timers on unmount", async () => {
  let resolve!: (s: Status) => void;
  read
    .mockResolvedValueOnce(connected)
    .mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    )
    .mockResolvedValue(disconnected);
  const view = await open();
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
  });
  const oldSignal = read.mock.calls[1][0];
  fireEvent.click(screen.getByRole("button", { name: "Sync repositories" }));
  await settle();
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => {
    resolve(connected);
  });
  expect(card().getByText("Not connected")).toBeVisible();
  const calls = read.mock.calls.length;
  view.unmount();
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(GITHUB_WAIT_MS);
  });
  expect(read).toHaveBeenCalledTimes(calls);
});

it("aborts a hanging status read, shows a timeout, and permits retry", async () => {
  read.mockImplementationOnce(
    (signal) =>
      new Promise((_, reject) => {
        signal!.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  );
  await open();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(GITHUB_REQUEST_MS);
  });
  expect(screen.getByRole("alert")).toHaveTextContent("status check timed out");
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await settle();
  expect(card().getByText("Not connected")).toBeVisible();
});

it("keeps a bridge credential distinct from a connected GitHub App", async () => {
  read.mockResolvedValue({ ...disconnected, bridgeToken: true });
  await open();
  expect(card().getByText("Credential available")).toBeVisible();
  expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeEnabled();
  expect(
    screen.queryByText("GitHub installation connected."),
  ).not.toBeInTheDocument();
});

it("recovers from a polling failure when the backend later confirms the installation", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  await settle();
  read
    .mockRejectedValueOnce(new Error("Temporary network failure"))
    .mockResolvedValue(connected);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Temporary network failure",
  );
  expect(card().getByText("Status unavailable")).toBeVisible();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(card().getByText("Connected")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("times out a hanging initiation and prevents duplicate clicks while opening GitHub", async () => {
  const begin = vi.spyOn(client, "beginGithubAuthorize").mockImplementationOnce(
    (signal) =>
      new Promise((_, reject) => {
        signal!.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  );
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
  expect(
    screen.getByRole("button", { name: "Opening GitHub…" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Opening GitHub…" }));
  expect(begin).toHaveBeenCalledOnce();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(GITHUB_REQUEST_MS);
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "GitHub request timed out",
  );
  expect(popup.close).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeEnabled();
});

it("clears the waiting timeout when a later focus read confirms connection", async () => {
  sessionStorage.setItem(GITHUB_PENDING_KEY, String(Date.now() + 2500));
  await open();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(screen.getByRole("alert")).toHaveTextContent("has not been confirmed");
  read.mockResolvedValue(connected);
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
  });
  expect(card().getByText("Connected")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
