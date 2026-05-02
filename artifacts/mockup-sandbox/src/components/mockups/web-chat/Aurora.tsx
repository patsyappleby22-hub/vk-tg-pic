import React, { useEffect, useState } from 'react';
import { 
  Plus, ImageIcon, Video, Music, MessageSquare, Search, 
  Settings, MoreVertical, Paperclip, Send, Play, Pause, 
  Trash2, Archive, Edit2, Sparkles, LayoutDashboard, 
  LogOut, Wallet, User, ChevronDown, Check, Volume2, Globe
} from 'lucide-react';

export function Aurora() {
  useEffect(() => {
    const link = document.createElement('link');
    link.href = 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&family=Syne:wght@500;600;700;800&display=swap';
    link.rel = 'stylesheet';
    document.head.appendChild(link);
    return () => {
      document.head.removeChild(link);
    };
  }, []);

  const [mode, setMode] = useState<'chat'|'image'|'video'|'music'>('image');

  return (
    <div 
      className="h-screen w-full flex text-slate-300 antialiased overflow-hidden selection:bg-indigo-500/30"
      style={{
        fontFamily: "'Inter', sans-serif",
        backgroundColor: '#050508',
        backgroundImage: `
          radial-gradient(circle at 15% 0%, rgba(99, 102, 241, 0.08) 0%, transparent 40%), 
          radial-gradient(circle at 85% 100%, rgba(168, 85, 247, 0.05) 0%, transparent 40%)
        `
      }}
    >
      {/* Ambient glowing orbs */}
      <div className="absolute top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-indigo-600/10 blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-purple-600/10 blur-[120px] pointer-events-none" />

      {/* Sidebar */}
      <aside className="w-[280px] flex flex-col bg-white/[0.02] border-r border-white/[0.04] backdrop-blur-2xl z-10 shrink-0">
        <div className="h-16 flex items-center px-5 border-b border-white/[0.04]">
          <div className="flex items-center gap-2 text-indigo-300">
            <Sparkles className="w-5 h-5" />
            <span className="text-lg tracking-tight font-bold" style={{ fontFamily: "'Syne', sans-serif" }}>PicGenAI</span>
          </div>
        </div>

        <div className="p-4 border-b border-white/[0.04]">
          <button className="w-full flex items-center justify-center gap-2 bg-indigo-500 hover:bg-indigo-400 text-white shadow-[0_0_20px_rgba(99,102,241,0.2)] transition-all py-2.5 px-4 rounded-xl font-medium text-sm">
            <Plus className="w-4 h-4" />
            Новый чат
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 py-4 space-y-6">
          <div>
            <button className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-white/[0.04] transition-colors text-sm font-medium">
              <LayoutDashboard className="w-4 h-4" />
              Лента работ
            </button>
          </div>

          <div>
            <div className="px-3 mb-2 text-xs font-semibold text-slate-500 uppercase tracking-wider">Недавние</div>
            <div className="space-y-0.5">
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5 text-blue-400" />} label="Постеры для кофейни" active />
              <ChatMenuItem icon={<Video className="w-3.5 h-3.5 text-purple-400" />} label="Сцена для рилса" />
              <ChatMenuItem icon={<Music className="w-3.5 h-3.5 text-emerald-400" />} label="Лоу-фай биты" />
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5 text-blue-400" />} label="Превью YouTube" />
              <ChatMenuItem icon={<MessageSquare className="w-3.5 h-3.5 text-slate-400" />} label="Сценарий ролика" />
              <ChatMenuItem icon={<ImageIcon className="w-3.5 h-3.5 text-blue-400" />} label="Логотип студии" />
            </div>
          </div>
        </div>

        <div className="p-4 border-t border-white/[0.04] space-y-4">
          <div className="bg-white/[0.03] border border-white/[0.05] rounded-xl p-3">
            <div className="flex justify-between items-center mb-2">
              <div className="text-xs text-slate-400 flex items-center gap-1.5">
                <Wallet className="w-3.5 h-3.5" /> Баланс
              </div>
              <div className="text-xs font-medium text-indigo-300" style={{ fontFamily: "'JetBrains Mono', monospace" }}>2 450 кр.</div>
            </div>
            <button className="w-full py-1.5 bg-white/[0.05] hover:bg-white/[0.1] text-slate-300 rounded-lg text-xs font-medium transition-colors">
              Пополнить
            </button>
          </div>

          <div className="flex items-center justify-between group cursor-pointer px-1">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center text-white font-medium text-sm shadow-lg">
                A
              </div>
              <div className="flex flex-col">
                <span className="text-sm font-medium text-slate-200">Alexey</span>
                <span className="text-xs text-slate-500">@alexey_art</span>
              </div>
            </div>
            <LogOut className="w-4 h-4 text-slate-500 group-hover:text-slate-300 transition-colors" />
          </div>
        </div>
      </aside>

      {/* Main Area */}
      <main className="flex-1 flex flex-col relative z-10">
        {/* Header */}
        <header className="h-16 flex items-center justify-between px-6 border-b border-white/[0.04] bg-white/[0.01] backdrop-blur-md shrink-0">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-medium text-slate-100">Постеры для кофейни</h1>
            <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">Flux 1.1 Pro</span>
          </div>
          
          <div className="flex items-center gap-1">
            <HeaderButton icon={<Edit2 className="w-4 h-4" />} title="Переименовать" />
            <HeaderButton icon={<Archive className="w-4 h-4" />} title="В архив" />
            <HeaderButton icon={<Trash2 className="w-4 h-4 text-red-400" />} title="Удалить" />
            <div className="w-px h-4 bg-white/10 mx-1" />
            <HeaderButton icon={<Settings className="w-4 h-4" />} title="Настройки" />
          </div>
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-8 scroll-smooth">
          <div className="max-w-4xl mx-auto w-full space-y-8 pb-4">
            
            {/* User message */}
            <div className="flex gap-4 justify-end">
              <div className="max-w-[70%]">
                <div className="bg-indigo-500/20 border border-indigo-500/30 text-indigo-50 px-5 py-3.5 rounded-2xl rounded-tr-sm shadow-sm backdrop-blur-sm">
                  <p className="text-sm leading-relaxed">
                    Сделай постер для спешелти-кофейни. Стиль минимализм, неоновый свет, темный фон. На переднем плане чашка капучино с идеальным латте-артом, от нее идет легкий пар.
                  </p>
                </div>
                <div className="text-[11px] text-slate-500 mt-1.5 text-right px-1">14:22</div>
              </div>
            </div>

            {/* Assistant message - text */}
            <div className="flex gap-4">
              <div className="w-8 h-8 rounded-full bg-white/[0.05] border border-white/[0.1] flex items-center justify-center shrink-0">
                <Sparkles className="w-4 h-4 text-indigo-400" />
              </div>
              <div className="max-w-[70%]">
                <div className="bg-white/[0.03] border border-white/[0.05] text-slate-300 px-5 py-3.5 rounded-2xl rounded-tl-sm shadow-sm">
                  <p className="text-sm leading-relaxed">
                    Отличная идея. Я подготовил генерацию постера в темном минималистичном стиле с неоновыми акцентами. Использована модель Flux 1.1 Pro для максимальной фотореалистичности латте-арта.
                  </p>
                </div>
                <div className="text-[11px] text-slate-500 mt-1.5 px-1">14:22</div>
              </div>
            </div>

            {/* Assistant message - image */}
            <div className="flex gap-4">
              <div className="w-8 h-8 rounded-full bg-white/[0.05] border border-white/[0.1] flex items-center justify-center shrink-0">
                <Sparkles className="w-4 h-4 text-indigo-400" />
              </div>
              <div className="max-w-[80%]">
                <div className="p-2 bg-white/[0.02] border border-white/[0.05] rounded-2xl rounded-tl-sm shadow-xl backdrop-blur-sm relative group">
                  <img 
                    src="/__mockup/images/aurora-gen.png" 
                    alt="Coffee poster" 
                    className="w-full max-w-md rounded-xl object-cover aspect-[4/5] bg-black/50"
                  />
                  <div className="absolute inset-2 rounded-xl bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-3 backdrop-blur-[2px]">
                    <button className="bg-white/10 hover:bg-white/20 text-white p-2.5 rounded-lg backdrop-blur-md transition-colors">
                      <Search className="w-5 h-5" />
                    </button>
                    <button className="bg-white/10 hover:bg-white/20 text-white px-4 py-2.5 rounded-lg backdrop-blur-md font-medium text-sm transition-colors">
                      Скачать (4K)
                    </button>
                  </div>
                  <div className="flex justify-between items-center mt-3 px-2 pb-1">
                    <div className="text-xs text-slate-400 font-medium">1024 × 1280 • Flux 1.1 Pro</div>
                    <div className="flex gap-2">
                      <button className="text-slate-400 hover:text-indigo-300 text-xs font-medium">Вариации</button>
                      <button className="text-slate-400 hover:text-indigo-300 text-xs font-medium">Увеличить</button>
                    </div>
                  </div>
                </div>
                <div className="text-[11px] text-slate-500 mt-1.5 px-1">14:24</div>
              </div>
            </div>

            {/* Assistant message - music */}
            <div className="flex gap-4">
              <div className="w-8 h-8 rounded-full bg-white/[0.05] border border-white/[0.1] flex items-center justify-center shrink-0">
                <Sparkles className="w-4 h-4 text-indigo-400" />
              </div>
              <div className="max-w-[60%] w-[400px]">
                <div className="bg-white/[0.03] border border-white/[0.05] p-4 rounded-2xl rounded-tl-sm shadow-sm flex flex-col gap-3">
                  <div className="text-sm text-slate-300">А вот фоновый лоу-фай трек для рилса с этим постером:</div>
                  <div className="flex items-center gap-3 bg-black/20 rounded-xl p-2.5 border border-white/[0.05]">
                    <button className="w-10 h-10 rounded-full bg-indigo-500/20 text-indigo-400 flex items-center justify-center hover:bg-indigo-500/30 transition-colors shrink-0">
                      <Play className="w-4 h-4 ml-0.5" />
                    </button>
                    <div className="flex-1 h-6 flex items-center gap-[2px] opacity-70">
                      {/* Fake waveform */}
                      {[...Array(30)].map((_, i) => (
                        <div key={i} className="w-1 bg-indigo-400/60 rounded-full" style={{ height: `${Math.max(20, Math.sin(i) * 100)}%` }} />
                      ))}
                    </div>
                    <div className="text-[11px] font-medium text-slate-400" style={{ fontFamily: "'JetBrains Mono', monospace" }}>0:45</div>
                  </div>
                  <div className="text-xs text-slate-500">Модель: Suno v3.5</div>
                </div>
                <div className="text-[11px] text-slate-500 mt-1.5 px-1">14:27</div>
              </div>
            </div>
            
          </div>
        </div>

        {/* Composer */}
        <div className="p-4 bg-white/[0.01] border-t border-white/[0.04] backdrop-blur-2xl shrink-0 flex flex-col items-center">
          <div className="max-w-4xl w-full flex flex-col gap-3 relative">
            
            {/* Top controls row */}
            <div className="flex items-center justify-between">
              <div className="flex bg-white/[0.03] border border-white/[0.05] rounded-lg p-1 gap-1">
                <ModeButton active={mode==='chat'} icon={<MessageSquare className="w-3.5 h-3.5" />} label="Чат" onClick={() => setMode('chat')} />
                <ModeButton active={mode==='image'} icon={<ImageIcon className="w-3.5 h-3.5" />} label="Фото" onClick={() => setMode('image')} />
                <ModeButton active={mode==='video'} icon={<Video className="w-3.5 h-3.5" />} label="Видео" onClick={() => setMode('video')} />
                <ModeButton active={mode==='music'} icon={<Music className="w-3.5 h-3.5" />} label="Музыка" onClick={() => setMode('music')} />
              </div>

              <div className="flex items-center gap-2">
                <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.05] rounded-lg px-3 py-1.5 cursor-pointer hover:bg-white/[0.05] transition-colors">
                  <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
                  <span className="text-xs font-medium text-slate-300">Flux 1.1 Pro</span>
                  <ChevronDown className="w-3 h-3 text-slate-500 ml-1" />
                </div>
                
                <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.05] rounded-lg px-3 py-1.5 cursor-pointer hover:bg-white/[0.05] transition-colors">
                  <span className="text-xs font-medium text-slate-300">16:9</span>
                  <ChevronDown className="w-3 h-3 text-slate-500 ml-1" />
                </div>

                <div className="flex items-center gap-2 bg-white/[0.03] border border-white/[0.05] rounded-lg px-3 py-1.5 cursor-pointer hover:bg-white/[0.05] transition-colors">
                  <Globe className="w-3.5 h-3.5 text-slate-400" />
                  <span className="text-xs font-medium text-slate-300">Поиск</span>
                </div>
              </div>
            </div>

            {/* Input area */}
            <div className="relative bg-black/40 border border-white/[0.08] focus-within:border-indigo-500/50 rounded-2xl shadow-inner transition-colors">
              <textarea 
                className="w-full bg-transparent text-sm text-slate-100 placeholder:text-slate-600 resize-none outline-none py-4 px-5 pr-32"
                rows={3}
                placeholder="Опишите, что сгенерировать… Например: кинематографичный кадр ночного города..."
                defaultValue=""
              />
              
              <div className="absolute right-3 bottom-3 flex items-center gap-2">
                <button className="p-2 text-slate-500 hover:text-slate-300 transition-colors rounded-lg hover:bg-white/5">
                  <Paperclip className="w-4 h-4" />
                </button>
                <button className="flex items-center justify-center gap-2 bg-indigo-500 hover:bg-indigo-400 text-white px-4 py-2 rounded-xl text-sm font-medium shadow-[0_0_15px_rgba(99,102,241,0.3)] transition-all">
                  <span>Отправить</span>
                  <Send className="w-3.5 h-3.5" />
                </button>
              </div>

              {/* Cost indicator */}
              <div className="absolute right-4 top-4 text-xs font-medium text-slate-500 flex items-center gap-1" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                ~ 5 кр.
              </div>
            </div>
            
            <div className="text-center text-[10px] text-slate-600 mt-1">
              AI может допускать ошибки. Проверяйте важную информацию.
            </div>
          </div>
        </div>

      </main>
    </div>
  );
}

function ChatMenuItem({ icon, label, active = false }: { icon: React.ReactNode, label: string, active?: boolean }) {
  return (
    <button className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-colors ${active ? 'bg-indigo-500/10 text-indigo-300' : 'text-slate-400 hover:text-slate-200 hover:bg-white/[0.03]'}`}>
      <div className="flex items-center gap-3 truncate">
        {icon}
        <span className="truncate font-medium">{label}</span>
      </div>
      {active && <MoreVertical className="w-3.5 h-3.5 opacity-50 shrink-0" />}
    </button>
  );
}

function HeaderButton({ icon, title }: { icon: React.ReactNode, title: string }) {
  return (
    <button 
      title={title}
      className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-200 hover:bg-white/[0.05] transition-colors"
    >
      {icon}
    </button>
  );
}

function ModeButton({ icon, label, active, onClick }: { icon: React.ReactNode, label: string, active: boolean, onClick: () => void }) {
  return (
    <button 
      onClick={onClick}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
        active 
          ? 'bg-white/10 text-white shadow-sm' 
          : 'text-slate-500 hover:text-slate-300 hover:bg-white/5'
      }`}
    >
      {icon}
      {label}
    </button>
  );
}
