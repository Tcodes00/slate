import { apiFetch } from './api';

// The backend pulls the tenant's real, current pipeline itself — this just
// sends the conversation. No API key touches the browser.
export async function callCopilot(conversation) {
  const data = await apiFetch('/api/copilot', {
    method: 'POST',
    body: JSON.stringify({ messages: conversation }),
  });
  return data.reply;
}
