import { useState } from "react";

import type { EpisodeItem } from "../domain/model";

interface EpisodePanelProps {
  episodes: EpisodeItem[];
}

export function EpisodePanel({ episodes }: EpisodePanelProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  return (
    <section className="side-section episode-section" aria-labelledby="episodes-title">
      <header className="section-heading compact">
        <div>
          <span className="eyebrow">LOCAL STORE</span>
          <h2 id="episodes-title">已落盘 Episodes</h2>
        </div>
        <span className="count-badge">{episodes.length}</span>
      </header>
      <div className="episode-list">
        {episodes.map((episode) => {
          const expanded = expandedId === episode.id;
          return (
            <button
              className={`episode-row ${expanded ? "is-expanded" : ""}`}
              key={episode.id}
              onClick={() => setExpandedId(expanded ? null : episode.id)}
              type="button"
            >
              <span className={`outcome-dot ${episode.outcome}`} aria-hidden="true" />
              <span className="episode-summary">
                <strong>{episode.startedAt}</strong>
                <span>{episode.durationSeconds.toFixed(1)} s · {episode.sizeGb.toFixed(2)} GB</span>
                {episode.warning && <span className="episode-warning">{episode.warning}</span>}
                {expanded && (
                  <span className="episode-detail">
                    <code>{episode.id}</code>
                    <code>{episode.path}</code>
                  </span>
                )}
              </span>
              <span className={`outcome-label ${episode.outcome}`}>{episode.outcome}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
