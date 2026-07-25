import type { ReactNode } from "react";

/** http(s) URL 및 www. 로 시작하는 주소를 링크로 분리 */
const URL_RE = /(https?:\/\/[^\s<>"'`]+|www\.[^\s<>"'`]+)/gi;

function splitUrlToken(raw: string): { core: string; trailing: string } {
  const m = raw.match(/^(.*?)([.,;:!?)]*)$/);
  return { core: m?.[1] ?? raw, trailing: m?.[2] ?? "" };
}

function toHref(core: string): string {
  if (/^https?:\/\//i.test(core)) return core;
  return `https://${core}`;
}

export function linkifyText(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  const re = new RegExp(URL_RE.source, URL_RE.flags);

  for (const match of text.matchAll(re)) {
    const raw = match[0];
    const index = match.index ?? 0;
    if (index > lastIndex) {
      nodes.push(text.slice(lastIndex, index));
    }
    const { core, trailing } = splitUrlToken(raw);
    if (core) {
      nodes.push(
        <a
          key={`url-${index}`}
          href={toHref(core)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-link"
        >
          {core}
        </a>,
      );
    }
    if (trailing) nodes.push(trailing);
    lastIndex = index + raw.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes.length > 0 ? nodes : [text];
}
