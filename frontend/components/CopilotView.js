'use client';

import { useEffect, useRef } from 'react';
import { Send } from 'lucide-react';

const QUICK_PROMPTS = [
  'Which deals need attention this week?',
  'Draft a follow-up email for the highest-value open deal',
  'Summarize the pipeline in three sentences',
];

export default function CopilotView({ messages, input, setInput, onSend, loading }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  return (
    <div className="slate-chat">
      <div className="slate-chat-log">
        {messages.map((m, i) => (
          <div className={`slate-chat-bubble ${m.role}`} key={i}>
            {m.content}
          </div>
        ))}
        {loading && (
          <div className="slate-chat-bubble assistant loading">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </div>
        )}
        <div ref={endRef} />
      </div>

      {messages.length <= 1 && (
        <div className="slate-chips">
          {QUICK_PROMPTS.map((p, i) => (
            <button className="slate-chip" key={i} onClick={() => onSend(p)}>
              {p}
            </button>
          ))}
        </div>
      )}

      <div className="slate-chat-input-row">
        <input
          className="slate-chat-input"
          placeholder="Ask about any deal, contact, or trend…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && input.trim()) onSend(input);
          }}
        />
        <button
          className="slate-btn slate-btn-primary"
          onClick={() => input.trim() && onSend(input)}
          disabled={loading || !input.trim()}
          aria-label="Send"
        >
          <Send size={15} />
        </button>
      </div>
    </div>
  );
}
