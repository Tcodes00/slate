'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { fetchFeatureFlags, updateFeatureFlag } from '../lib/api';

const FlagsContext = createContext({
  flags: [], loading: true, isOn: () => true, refresh: () => {}, setFlag: async () => {},
});

export function FeatureFlagsProvider({ children }) {
  const [flags, setFlags] = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(
    () => fetchFeatureFlags().then(setFlags).catch(() => setFlags([])).finally(() => setLoading(false)),
    [],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  const isOn = useCallback((key) => {
    const f = flags.find((x) => x.key === key);
    // Fail-open while loading / for any key not yet known — matches the
    // server's own registry defaults, so there's no UI flash-of-disabled
    // before the real state loads in.
    return f ? f.enabled : true;
  }, [flags]);

  const setFlag = useCallback(async (key, enabled) => {
    await updateFeatureFlag(key, enabled);
    await refresh();
  }, [refresh]);

  return (
    <FlagsContext.Provider value={{ flags, loading, isOn, refresh, setFlag }}>
      {children}
    </FlagsContext.Provider>
  );
}

export function useFeatureFlags() {
  return useContext(FlagsContext);
}

export function useFeatureFlag(key) {
  return useContext(FlagsContext).isOn(key);
}
