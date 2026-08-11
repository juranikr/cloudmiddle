import { useEffect, useRef, useState, type FormEvent } from "react";
import * as api from "../api";
import type { City, MarkerItem, TravelChatMessage } from "../types";
import BrandMark from "./BrandMark";

const QUICK = ["박물관 말고 선양다운 반나절을 짜줘", "오늘 밤 갈 만한 시장과 먹거리를 골라줘", "내 일정의 이동 동선을 줄여줘", "이 지도에서 빠진 현지 동네를 찾아줘"];

export default function TravelAgent({ token, city, selected, onOpen }: { token: string; city: City; selected: MarkerItem | null; onOpen: (placeId: number) => void }) {
  const [messages, setMessages] = useState<TravelChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { void api.fetchTravelChat(token, city.id).then(setMessages).catch(() => setMessages([])); }, [token, city.id]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  async function submit(e?: FormEvent, quick?: string) {
    e?.preventDefault();
    const message = (quick ?? text).trim(); if (!message || busy) return;
    setBusy(true); setError(""); setText("");
    const optimistic: TravelChatMessage = { id: -Date.now(), city_id: city.id, role: "user", content: message, sources: [], place_ids: [], created_at: new Date().toISOString() };
    setMessages((current) => [...current, optimistic]);
    try {
      const result = await api.sendTravelChat(token, { city_id: city.id, message, selected_place_id: selected?.id });
      setMessages((current) => [...current, result.message]);
    } catch (e2) { setError(e2 instanceof Error ? e2.message : "여행 대화를 이어가지 못했습니다."); }
    finally { setBusy(false); }
  }

  return (
    <main className="workspace-screen travel-agent">
      <header className="workspace-screen__header"><BrandMark /><div><span>MAP-GROUNDED TRAVEL COMPANION</span><h1>{city.name_ko}을 함께 걷는 대화</h1><p>저장된 장소와 일정부터 읽고, 필요할 때만 새 정보를 찾아요.</p></div></header>
      <section className="agent-layout">
        <aside><strong>무엇을 도와드릴까요?</strong>{QUICK.map((prompt) => <button key={prompt} type="button" onClick={() => void submit(undefined, prompt)}>{prompt}</button>)}{selected ? <div><small>지금 보고 있는 장소</small><b>{selected.title}</b><span>장소 #{selected.id}</span></div> : null}</aside>
        <section className="agent-chat">
          <div className="agent-chat__messages">{messages.length === 0 ? <div className="agent-welcome"><i>遠</i><strong>지도에 있는 것부터 물어보세요.</strong><p>예: “서탑을 저녁에 넣고 근처에서 뭘 먹지?”</p></div> : null}{messages.map((message) => <article key={message.id} className={`agent-message agent-message--${message.role}`}><small>{message.role === "assistant" ? "WONRAE" : "나"}</small><p>{message.content}</p>{message.place_ids.length ? <div>{message.place_ids.map((id) => <button key={id} type="button" onClick={() => onOpen(id)}>장소 #{id} 지도에서 보기</button>)}</div> : null}{message.sources.length ? <details><summary>확인한 출처 {message.sources.length}</summary>{message.sources.map((source, index) => <a key={source} href={source} target="_blank" rel="noreferrer">출처 {index + 1}</a>)}</details> : null}</article>)}{busy ? <div className="agent-thinking"><i /><i /><i /><span>지도와 최신 정보를 함께 보고 있어요</span></div> : null}<div ref={endRef} /></div>
          {error ? <p className="workspace-error">{error}</p> : null}
          <form className="agent-compose" onSubmit={(e) => void submit(e)}><textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="장소, 동선, 음식, 최신 방문 정보를 물어보세요" rows={2} /><button type="submit" disabled={busy || !text.trim()}>보내기</button><small>지도 추가를 명시하면 근거를 확인해 관리자 승인 후보로 저장합니다.</small></form>
        </section>
      </section>
    </main>
  );
}
