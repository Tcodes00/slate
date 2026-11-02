'use client';

import { useEffect, useState } from 'react';
import { clearToken, fetchMe, getToken } from '../lib/api';
import LoginScreen from '../components/LoginScreen';
import SlateApp from '../components/SlateApp';

export default function Page() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      setChecking(false);
      return;
    }
    fetchMe()
      .then(setUser)
      .catch(() => clearToken())
      .finally(() => setChecking(false));
  }, []);

  function handleLogout() {
    clearToken();
    setUser(null);
  }

  if (checking) return <div className="slate-loading-screen">Loading…</div>;
  if (!user) return <LoginScreen onAuth={setUser} />;
  return <SlateApp user={user} onLogout={handleLogout} onUserUpdate={setUser} />;
}
