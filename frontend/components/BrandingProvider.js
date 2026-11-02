'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { fetchBranding, fetchLogoBlobUrl } from '../lib/api';
import { deriveShades } from '../lib/color';

const DEFAULTS = {
  display_name: 'Slate', has_logo: false, primary_color: '#0E7C6B', secondary_color: '#B8792E', is_customized: false,
};

const BrandingContext = createContext({ ...DEFAULTS, logoSrc: null, loading: true, reload: async () => {} });

export function BrandingProvider({ children }) {
  const [branding, setBranding] = useState(DEFAULTS);
  const [logoSrc, setLogoSrc] = useState(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const data = await fetchBranding();
      setBranding(data);
      setLogoSrc(data.has_logo ? await fetchLogoBlobUrl() : null);
    } catch {
      setBranding(DEFAULTS);
      setLogoSrc(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    const root = document.documentElement;
    const { dark, soft } = deriveShades(branding.primary_color);
    root.style.setProperty('--accent', branding.primary_color);
    root.style.setProperty('--accent-dark', dark);
    root.style.setProperty('--accent-soft', soft);
    root.style.setProperty('--brand-secondary', branding.secondary_color);
    document.title = branding.display_name || 'Slate';
  }, [branding]);

  return (
    <BrandingContext.Provider value={{ ...branding, logoSrc, loading, reload }}>
      {children}
    </BrandingContext.Provider>
  );
}

export function useBranding() {
  return useContext(BrandingContext);
}
