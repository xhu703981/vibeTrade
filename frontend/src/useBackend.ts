// WebSocket /api/events (live events) + polling /api/state (panel data).
import { useCallback, useEffect, useRef, useState } from "react";
import type { Api } from "./api";
import type { AppState, BackendEvent } from "./types";

export function wsUrl(path = "/api/events"): string {
  const { protocol, host } = window.location;
  return `${protocol === "https:" ? "wss" : "ws"}://${host}${path}`;
}

export function useBackend(api: Api, onLiveEvent: (ev: BackendEvent) => void) {
  const [events, setEvents] = useState<BackendEvent[]>([]);
  const [state, setState] = useState<AppState | null>(null);
  const [wsUp, setWsUp] = useState(false);
  const live = useRef(onLiveEvent);
  live.current = onLiveEvent;

  const refresh = useCallback(async () => {
    try {
      setState((await api.get("/state")) as AppState);
    } catch {
      /* backend not up: keep the old state */
    }
  }, [api]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stop = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => setWsUp(true);
      ws.onclose = () => {
        setWsUp(false);
        if (!stop) retry = setTimeout(connect, 1500);
      };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.kind === "hello") {
          setEvents((msg.history as BackendEvent[]).map((e) => ({ ...e, replay: true })));
        } else if (msg.kind === "event") {
          const ev = msg as BackendEvent;
          if (ev.type === "session_reset") {   // a new session: earlier sessions' events are gone
            setEvents([]);
            refresh();
            return;
          }
          setEvents((xs) => (xs.some((x) => x.id === ev.id) ? xs : [...xs.slice(-299), ev]));
          live.current(ev);
          refresh();
        }
      };
    };
    connect();
    return () => {
      stop = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [refresh]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [refresh]);

  return { events, state, wsUp, refresh };
}

export function useNow(ms = 500) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
