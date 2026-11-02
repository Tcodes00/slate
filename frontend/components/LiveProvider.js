'use client';

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { getToken, wsUrl } from '../lib/api';

const LiveContext = createContext({ connected: false, subscribe: () => () => {} });

export function LiveProvider({ enabled, children }) {
  const [connected, setConnected] = useState(false);
  const listenersRef = useRef(new Set());
  const socketRef = useRef(null);
  const retryRef = useRef(0);
  const closedByUsRef = useRef(false);

  const subscribe = useCallback((handler) => {
    listenersRef.current.add(handler);
    return () => listenersRef.current.delete(handler);
  }, []);

  useEffect(() => {
    if (!enabled || !getToken()) {
      setConnected(false);
      return undefined;
    }
    closedByUsRef.current = false;
    let keepAlive;

    function connect() {
      const ws = new WebSocket(wsUrl());
      socketRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
        setConnected(true);
      };
      ws.onmessage = (event) => {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }
        listenersRef.current.forEach((fn) => fn(msg));
      };
      ws.onclose = () => {
        setConnected(false);
        if (closedByUsRef.current) return;
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
        retryRef.current += 1;
        setTimeout(connect, delay);
      };
      ws.onerror = () => ws.close();
    }

    connect();
    keepAlive = setInterval(() => {
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send('ping');
    }, 25000);

    return () => {
      closedByUsRef.current = true;
      clearInterval(keepAlive);
      socketRef.current?.close();
    };
  }, [enabled]);

  return <LiveContext.Provider value={{ connected, subscribe }}>{children}</LiveContext.Provider>;
}

export function useLive() {
  return useContext(LiveContext);
}

export function useLiveEvent(eventType, handler) {
  const { subscribe } = useLive();
  useEffect(() => subscribe((msg) => {
    if (msg.type === eventType) handler(msg.data);
  }), [eventType, handler, subscribe]);
}
