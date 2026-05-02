import React, { useEffect, useState } from 'react';
import {
  Plus, ImageIcon, Video, Music, MessageSquare, Search,
  Settings, MoreVertical, Paperclip, Send, Play,
  Trash2, Archive, Edit2, Sparkles, LayoutDashboard,
  LogOut, Wallet, ChevronDown, Globe
} from 'lucide-react';

const NOISE_SVG =
  "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\")";

export function Aurora() {
  useEffect(() => {
    const link = document.createElement('link');
    link.href = 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&family=Syne:wght@500;600;700;800&display=swap';
    link.rel = 'stylesheet';
    document.head.appendChild(link);
    return () => { document.head.removeChild(link); };
  }, []);

  const [mode, setMode] = useState<'chat'|'image'|'video'|'music'>('image');

  return (
    <div
      className="relative h-screen w-full flex antialiased overflow-hidden"
      style={{
        fontFamily: "'Inter', sans-serif",
        backgroundColor: '#050507',
        color: '#ededf2',
      }}
    >
      <style>{`
        @keyframes orbFloat1 { 0%,100% { transform: translateX(-50%) translateY(0); } 50% { transform: translateX(-50%) translateY(-30px); } }
        @keyframes orbFloat2 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-20px); } }
        @keyframes orbFloat3 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(20px); } }
        @keyframes badgePulse { 0%,100% { opacity: 1; box-shadow: 0 0 8px #9b8afb; } 50% { opacity: 0.35; box-shadow: 0 0 3px #9b8afb; } }
        .aurora-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
        .aurora-scroll::-webkit-scrollbar-track { background: transparent; }
        .aurora-scroll::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.06); border-radius: 999px; }
        .aurora-scroll::-webkit-scrollbar-thumb:hover { background: rgba(155,138,251,0.25); }
        ::selection { background: rgba(155,138,251,0.28); color: #fff; }
      `}</style>

      {/* ─── Ambient orbs (landing-matched) ─── */}
      <div
        aria-hidden
        className="pointer-events-none absolute rounded-full"
        style={{
          width: 600, height: 600,
          top: -160, left: '50%',
          transform: 'translateX(-50%)',
          background: 'radial-gradient(circle, rgba(100,80,210,0.14) 0%, transparent 70%)',
          filter: 'blur(80px)',
          animation: 'orbFloat1 8s ease-in-out infinite',
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute rounded-full"
        style={{
          width: 400, height: 400,
          bottom: -120, right: -80,
          background: 'radial-gradient(circle, rgba(80,60,180,0.09) 0%, transparent 70%)',
          filter: 'blur(80px)',
          animation: 'orbFloat2 10s ease-in-out infinite reverse',
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute rounded-full"
        style={{
          width: 300, height: 300,
          top: '32%', left: -100,
          background: 'radial-gradient(circle, rgba(120,100,230,0.07) 0%, transparent 70%)',
          filter: 'blur(80px)',
          animation: 'orbFloat3 12s ease-in-out infinite 2s',
        }}
      />

      {/* ─── Sidebar ─── */}
      <aside
        className="relative w-[280px] shrink-0 flex flex-col z-10"
        style={{
          background: 'rgba(10,10,14,0.55)',
          borderRight: '1px solid rgba(255,255,255,0.055)',
          backdropFilter: 'blur(24px)',
          WebkitBackdropFilter: 'blur(24px)',
        }}
      >
        {/* Logo */}
        <div
          className="h-16 flex items-center px-5"
          style={{ borderBottom: '1px solid rgba(255,255,255,0.055)' }}
        >
          <div className="flex items-center gap-2">
            <div className="relative">
              <span
                className="absolute inset-0 -m-1 rounded-full"
                style={{ background: 'rgba(155,138,251,0.18)', filter: 'blur(8px)' }}
              />
              <Sparkles className="relative w-5 h-5" style={{ color: '#b8acff' }} />
            </div>
            <span
              className="text-[1.05em] tracking-[0.01em] font-bold"
              style={{ fontFamily: "'Syne', sans-serif", color: '#ededf2' }}
            >
              Pic<span style={{ color: '#9b8afb' }}>GenAI</span>
            </span>
          </div>
        </div>

        {/* New chat */}
        <div className="p-4" style={{ borderBottom: '1px solid rgba(255,255,255,0.055)' }}>
          <button
            className="w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-[10px] text-[0.83em] font-semibold transition-all"
            style={{
              background: '#9b8afb',
              color: '#fff',
              boxShadow: '0 14px 32px rgba(155,138,251,0.22)',
              letterSpacing: '0.01em',
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#b8acff'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#9b8afb'; }}
          >
            <Plus className="w-4 h-4" />
            Новый чат
          </button>
        </div>

        {/* Nav + recent */}
        <div className="aurora-scroll flex-1 overflow-y-auto px-3 py-4 space-y-6">
          <div>
            <NavRow icon={<LayoutDashboard className="w-4 h-4" />} label="Лента работ" />
          </div>

          <div>
            <div
              className="px-3 mb-2 text-[0.66em] font-medium uppercase"
              style={{ letterSpacing: '0.13em', color: '#9b8afb', opacity: 0.8 }}
            >
              История
            </div>
            <div className="space-y-0.5">
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5" />} label="Постеры для кофейни" active />
              <ChatMenuItem icon={<Video className="w-3.5 h-3.5" />} label="Сцена для рилса" />
              <ChatMenuItem icon={<Music className="w-3.5 h-3.5" />} label="Лоу-фай биты" />
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5" />} label="Превью YouTube" />
              <ChatMenuItem icon={<MessageSquare className="w-3.5 h-3.5" />} label="Сценарий ролика" />
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5" />} label="Логотип студии" />
            </div>
          </div>
        </div>

        {/* Footer: balance + user */}
        <div className="p-4 space-y-4" style={{ borderTop: '1px solid rgba(255,255,255,0.055)' }}>
          <div
            className="rounded-[14px] p-3"
            style={{
              background: 'linear-gradient(160deg, rgba(155,138,251,0.05), rgba(15,15,20,0.6))',
              border: '1px solid rgba(155,138,251,0.18)',
            }}
          >
            <div className="flex justify-between items-center mb-2">
              <div className="text-[0.72em] flex items-center gap-1.5" style={{ color: '#7a7a96' }}>
                <Wallet className="w-3.5 h-3.5" /> Баланс
              </div>
              <div
                className="text-[0.78em] font-medium"
                style={{ fontFamily: "'JetBrains Mono', monospace", color: '#b8acff' }}
              >
                2 450 кр.
              </div>
            </div>
            <button
              className="w-full py-1.5 rounded-[8px] text-[0.75em] font-medium transition-colors"
              style={{
                background: 'rgba(255,255,255,0.04)',
                color: '#ededf2',
                border: '1px solid rgba(255,255,255,0.09)',
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = 'rgba(155,138,251,0.07)';
                (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(155,138,251,0.3)';
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = 'rgba(255,255,255,0.04)';
                (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(255,255,255,0.09)';
              }}
            >
              Пополнить
            </button>
          </div>

          <div className="flex items-center justify-between group cursor-pointer px-1">
            <div className="flex items-center gap-3">
              <div
                className="w-8 h-8 rounded-full flex items-center justify-center text-white text-[0.85em] font-semibold"
                style={{
                  background: 'linear-gradient(135deg, #9b8afb, #6b5dd1)',
                  fontFamily: "'Syne', sans-serif",
                  boxShadow: '0 6px 18px rgba(155,138,251,0.25)',
                }}
              >
                A
              </div>
              <div className="flex flex-col leading-tight">
                <span className="text-[0.82em] font-medium" style={{ color: '#ededf2' }}>Alexey</span>
                <span className="text-[0.7em]" style={{ color: '#52526a' }}>@alexey_art</span>
              </div>
            </div>
            <LogOut
              className="w-4 h-4 transition-colors"
              style={{ color: '#52526a' }}
            />
          </div>
        </div>
      </aside>

      {/* ─── Main ─── */}
      <main className="relative flex-1 flex flex-col z-10 min-w-0">
        {/* Header */}
        <header
          className="h-16 shrink-0 flex items-center justify-between px-6"
          style={{
            background: 'rgba(5,5,7,0.55)',
            borderBottom: '1px solid rgba(255,255,255,0.055)',
            backdropFilter: 'blur(18px)',
            WebkitBackdropFilter: 'blur(18px)',
          }}
        >
          <div className="flex items-center gap-3">
            <h1
              className="text-[1.0em] font-medium"
              style={{ fontFamily: "'Syne', sans-serif", color: '#ededf2', letterSpacing: '-0.01em' }}
            >
              Постеры для кофейни
            </h1>
            <span
              className="px-2 py-0.5 rounded text-[0.62em] font-medium"
              style={{
                background: 'rgba(155,138,251,0.10)',
                color: '#b8acff',
                border: '1px solid rgba(155,138,251,0.25)',
                letterSpacing: '0.04em',
              }}
            >
              FLUX 1.1 PRO
            </span>
          </div>

          <div className="flex items-center gap-1">
            <HeaderButton icon={<Edit2 className="w-4 h-4" />} title="Переименовать" />
            <HeaderButton icon={<Archive className="w-4 h-4" />} title="В архив" />
            <HeaderButton icon={<Trash2 className="w-4 h-4" />} title="Удалить" danger />
            <div className="w-px h-4 mx-1" style={{ background: 'rgba(255,255,255,0.09)' }} />
            <HeaderButton icon={<Settings className="w-4 h-4" />} title="Настройки" />
          </div>
        </header>

        {/* Messages */}
        <div className="aurora-scroll flex-1 overflow-y-auto px-6 py-8">
          <div className="max-w-[760px] mx-auto w-full space-y-8 pb-4">
            {/* User */}
            <div className="flex justify-end">
              <div className="max-w-[78%]">
                <div
                  className="px-5 py-3.5 rounded-[18px] rounded-tr-[6px]"
                  style={{
                    background: 'linear-gradient(160deg, rgba(155,138,251,0.16), rgba(155,138,251,0.06))',
                    border: '1px solid rgba(155,138,251,0.22)',
                    color: '#ededf2',
                  }}
                >
                  <p className="text-[0.86em] leading-relaxed font-light">
                    Сделай постер для спешелти-кофейни. Стиль минимализм, неоновый свет, темный фон. На переднем плане чашка капучино с идеальным латте-артом, от нее идет легкий пар.
                  </p>
                </div>
                <div className="text-[0.65em] mt-1.5 text-right pr-1" style={{ color: '#52526a' }}>14:22</div>
              </div>
            </div>

            {/* Assistant - text */}
            <div className="flex gap-4">
              <AssistantAvatar />
              <div className="max-w-[78%]">
                <div
                  className="px-5 py-3.5 rounded-[18px] rounded-tl-[6px]"
                  style={{
                    background: 'rgba(15,15,20,0.7)',
                    border: '1px solid rgba(255,255,255,0.055)',
                    color: '#ededf2',
                  }}
                >
                  <p className="text-[0.86em] leading-relaxed font-light" style={{ color: '#c8c8d8' }}>
                    Принято. Подготовил постер в темном минимализме с неоновыми акцентами. Использую Flux 1.1 Pro для максимальной фотореалистичности латте-арта.
                  </p>
                </div>
                <div className="text-[0.65em] mt-1.5 pl-1" style={{ color: '#52526a' }}>14:22</div>
              </div>
            </div>

            {/* Assistant - image */}
            <div className="flex gap-4">
              <AssistantAvatar />
              <div className="max-w-[80%]">
                <div
                  className="p-2 rounded-[18px] rounded-tl-[6px] relative group"
                  style={{
                    background: 'rgba(15,15,20,0.7)',
                    border: '1px solid rgba(255,255,255,0.055)',
                  }}
                >
                  <img
                    src="/__mockup/images/aurora-gen.png"
                    alt="Coffee poster"
                    className="w-full max-w-md rounded-[12px] object-cover aspect-[4/5]"
                    style={{ background: 'rgba(0,0,0,0.5)' }}
                  />
                  <div
                    className="absolute inset-2 rounded-[12px] opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-3"
                    style={{ background: 'rgba(0,0,0,0.45)', backdropFilter: 'blur(2px)' }}
                  >
                    <button
                      className="p-2.5 rounded-[10px] transition-colors"
                      style={{ background: 'rgba(255,255,255,0.10)', color: '#ededf2' }}
                    >
                      <Search className="w-5 h-5" />
                    </button>
                    <button
                      className="px-4 py-2.5 rounded-[10px] text-[0.82em] font-medium transition-colors"
                      style={{ background: 'rgba(255,255,255,0.10)', color: '#ededf2' }}
                    >
                      Скачать (4K)
                    </button>
                  </div>
                  <div className="flex justify-between items-center mt-3 px-2 pb-1">
                    <div
                      className="text-[0.7em]"
                      style={{ fontFamily: "'JetBrains Mono', monospace", color: '#7a7a96' }}
                    >
                      1024 × 1280 · Flux 1.1 Pro
                    </div>
                    <div className="flex gap-3">
                      <button className="text-[0.74em] font-medium" style={{ color: '#9b8afb' }}>Вариации</button>
                      <button className="text-[0.74em] font-medium" style={{ color: '#9b8afb' }}>Увеличить</button>
                    </div>
                  </div>
                </div>
                <div className="text-[0.65em] mt-1.5 pl-1" style={{ color: '#52526a' }}>14:24</div>
              </div>
            </div>

            {/* Assistant - music */}
            <div className="flex gap-4">
              <AssistantAvatar />
              <div className="w-[400px] max-w-[78%]">
                <div
                  className="p-4 rounded-[18px] rounded-tl-[6px] flex flex-col gap-3"
                  style={{
                    background: 'rgba(15,15,20,0.7)',
                    border: '1px solid rgba(255,255,255,0.055)',
                  }}
                >
                  <div className="text-[0.84em] font-light" style={{ color: '#c8c8d8' }}>
                    А вот фоновый лоу-фай трек для рилса с этим постером:
                  </div>
                  <div
                    className="flex items-center gap-3 rounded-[12px] p-2.5"
                    style={{
                      background: 'rgba(5,5,7,0.6)',
                      border: '1px solid rgba(255,255,255,0.055)',
                    }}
                  >
                    <button
                      className="w-10 h-10 rounded-full flex items-center justify-center shrink-0"
                      style={{
                        background: 'rgba(155,138,251,0.16)',
                        color: '#b8acff',
                        border: '1px solid rgba(155,138,251,0.25)',
                      }}
                    >
                      <Play className="w-4 h-4 ml-0.5" />
                    </button>
                    <div className="flex-1 h-6 flex items-center gap-[2px]">
                      {[...Array(34)].map((_, i) => {
                        const h = Math.max(18, Math.abs(Math.sin(i * 0.6)) * 100);
                        return (
                          <div
                            key={i}
                            className="w-1 rounded-full"
                            style={{ height: `${h}%`, background: i < 12 ? '#b8acff' : 'rgba(155,138,251,0.35)' }}
                          />
                        );
                      })}
                    </div>
                    <div
                      className="text-[0.68em] font-medium"
                      style={{ fontFamily: "'JetBrains Mono', monospace", color: '#7a7a96' }}
                    >
                      0:45
                    </div>
                  </div>
                  <div className="text-[0.7em]" style={{ color: '#52526a' }}>Модель: Suno v3.5</div>
                </div>
                <div className="text-[0.65em] mt-1.5 pl-1" style={{ color: '#52526a' }}>14:27</div>
              </div>
            </div>
          </div>
        </div>

        {/* Composer */}
        <div
          className="shrink-0 px-4 pt-4 pb-3 flex flex-col items-center"
          style={{
            background: 'rgba(5,5,7,0.55)',
            borderTop: '1px solid rgba(255,255,255,0.055)',
            backdropFilter: 'blur(18px)',
            WebkitBackdropFilter: 'blur(18px)',
          }}
        >
          <div className="max-w-[760px] w-full flex flex-col gap-3">
            {/* Top controls */}
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div
                className="flex p-1 gap-1 rounded-[10px]"
                style={{
                  background: 'rgba(15,15,20,0.6)',
                  border: '1px solid rgba(255,255,255,0.055)',
                }}
              >
                <ModeButton active={mode==='chat'}  icon={<MessageSquare className="w-3.5 h-3.5" />} label="Чат"    onClick={() => setMode('chat')} />
                <ModeButton active={mode==='image'} icon={<ImageIcon  className="w-3.5 h-3.5" />} label="Фото"   onClick={() => setMode('image')} />
                <ModeButton active={mode==='video'} icon={<Video      className="w-3.5 h-3.5" />} label="Видео"  onClick={() => setMode('video')} />
                <ModeButton active={mode==='music'} icon={<Music      className="w-3.5 h-3.5" />} label="Музыка" onClick={() => setMode('music')} />
              </div>

              <div className="flex items-center gap-2">
                <ComposerChip>
                  <Sparkles className="w-3.5 h-3.5" style={{ color: '#9b8afb' }} />
                  <span>Flux 1.1 Pro</span>
                  <ChevronDown className="w-3 h-3 opacity-60" />
                </ComposerChip>
                <ComposerChip>
                  <span>16:9</span>
                  <ChevronDown className="w-3 h-3 opacity-60" />
                </ComposerChip>
                <ComposerChip>
                  <Globe className="w-3.5 h-3.5 opacity-70" />
                  <span>Поиск</span>
                </ComposerChip>
              </div>
            </div>

            {/* Input */}
            <div
              className="relative rounded-[16px] transition-colors"
              style={{
                background: 'rgba(10,10,14,0.85)',
                border: '1px solid rgba(255,255,255,0.09)',
                boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.02)',
              }}
            >
              <textarea
                className="w-full bg-transparent text-[0.88em] resize-none outline-none py-4 px-5 pr-32 font-light"
                style={{ color: '#ededf2' }}
                rows={3}
                placeholder="Опишите, что сгенерировать… Например: кинематографичный кадр ночного города…"
                defaultValue=""
              />

              <div className="absolute right-3 bottom-3 flex items-center gap-2">
                <button
                  className="p-2 rounded-[8px] transition-colors"
                  style={{ color: '#7a7a96' }}
                >
                  <Paperclip className="w-4 h-4" />
                </button>
                <button
                  className="flex items-center justify-center gap-2 px-4 py-2 rounded-[10px] text-[0.82em] font-semibold transition-all"
                  style={{
                    background: '#9b8afb',
                    color: '#fff',
                    boxShadow: '0 12px 28px rgba(155,138,251,0.25)',
                  }}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#b8acff'; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#9b8afb'; }}
                >
                  <span>Отправить</span>
                  <Send className="w-3.5 h-3.5" />
                </button>
              </div>

              <div
                className="absolute right-4 top-3 text-[0.7em] flex items-center gap-1"
                style={{ fontFamily: "'JetBrains Mono', monospace", color: '#52526a' }}
              >
                ~ 5 кр.
              </div>
            </div>

            <div className="text-center text-[0.66em]" style={{ color: '#52526a', letterSpacing: '0.02em' }}>
              AI может допускать ошибки. Проверяйте важную информацию.
            </div>
          </div>
        </div>
      </main>

      {/* ─── Noise overlay (top, non-interactive) ─── */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          backgroundImage: NOISE_SVG,
          opacity: 0.6,
          zIndex: 9999,
          mixBlendMode: 'overlay',
        }}
      />
    </div>
  );
}

function NavRow({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <button
      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-[8px] text-[0.82em] font-medium transition-colors"
      style={{ color: '#7a7a96' }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = 'rgba(155,138,251,0.06)';
        (e.currentTarget as HTMLButtonElement).style.color = '#ededf2';
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
        (e.currentTarget as HTMLButtonElement).style.color = '#7a7a96';
      }}
    >
      {icon}
      {label}
    </button>
  );
}

function ChatMenuItem({ icon, label, active = false }: { icon: React.ReactNode; label: string; active?: boolean }) {
  return (
    <button
      className="w-full flex items-center justify-between px-3 py-2 rounded-[8px] text-[0.82em] transition-colors group"
      style={{
        background: active ? 'rgba(155,138,251,0.08)' : 'transparent',
        color: active ? '#ededf2' : '#7a7a96',
        border: active ? '1px solid rgba(155,138,251,0.18)' : '1px solid transparent',
      }}
    >
      <div className="flex items-center gap-3 truncate">
        <span style={{ color: active ? '#9b8afb' : '#52526a' }}>{icon}</span>
        <span className="truncate font-medium">{label}</span>
      </div>
      {active && <MoreVertical className="w-3.5 h-3.5 opacity-50 shrink-0" />}
    </button>
  );
}

function HeaderButton({ icon, title, danger = false }: { icon: React.ReactNode; title: string; danger?: boolean }) {
  return (
    <button
      title={title}
      className="w-8 h-8 flex items-center justify-center rounded-[8px] transition-colors"
      style={{ color: danger ? '#e57380' : '#7a7a96' }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = 'rgba(255,255,255,0.04)';
        if (!danger) (e.currentTarget as HTMLButtonElement).style.color = '#ededf2';
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
        if (!danger) (e.currentTarget as HTMLButtonElement).style.color = '#7a7a96';
      }}
    >
      {icon}
    </button>
  );
}

function ModeButton({ icon, label, active, onClick }: { icon: React.ReactNode; label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[0.74em] font-medium transition-all"
      style={{
        background: active ? 'rgba(155,138,251,0.12)' : 'transparent',
        color: active ? '#ededf2' : '#7a7a96',
        border: active ? '1px solid rgba(155,138,251,0.22)' : '1px solid transparent',
      }}
    >
      {icon}
      {label}
    </button>
  );
}

function ComposerChip({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[0.74em] font-medium cursor-pointer transition-colors"
      style={{
        background: 'rgba(15,15,20,0.6)',
        border: '1px solid rgba(255,255,255,0.055)',
        color: '#ededf2',
      }}
    >
      {children}
    </div>
  );
}

function AssistantAvatar() {
  return (
    <div
      className="w-8 h-8 rounded-full flex items-center justify-center shrink-0 relative"
      style={{
        background: 'rgba(155,138,251,0.10)',
        border: '1px solid rgba(155,138,251,0.25)',
      }}
    >
      <span
        className="absolute inset-0 rounded-full"
        style={{ background: 'rgba(155,138,251,0.18)', filter: 'blur(8px)', opacity: 0.6 }}
      />
      <Sparkles className="relative w-4 h-4" style={{ color: '#b8acff' }} />
    </div>
  );
}
