import React, { useEffect } from "react";
import {
  MessageSquare,
  Image as ImageIcon,
  Video,
  Music,
  Plus,
  Settings,
  MoreVertical,
  Search,
  Zap,
  Play,
  Paperclip,
  Send,
  Trash2,
  Archive,
  Edit2,
  LogOut,
  Hash,
  Activity,
  Layers,
  Globe,
  SlidersHorizontal,
  ChevronDown
} from "lucide-react";

export function Studio() {
  useEffect(() => {
    // Inject fonts
    const link = document.createElement("link");
    link.href = "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Inter:wght@400;500;600&family=Syne:wght@500;600;700&display=swap";
    link.rel = "stylesheet";
    document.head.appendChild(link);
    return () => {
      document.head.removeChild(link);
    };
  }, []);

  return (
    <div 
      className="min-h-screen w-full flex flex-col md:flex-row text-[#e4e4e7] selection:bg-indigo-500/30 overflow-hidden"
      style={{ 
        backgroundColor: "#09090b",
        fontFamily: "'Inter', sans-serif" 
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: `
        .font-display { font-family: 'Syne', sans-serif; }
        .font-mono { font-family: 'JetBrains Mono', monospace; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 0px; }
        ::-webkit-scrollbar-thumb:hover { background: #3f3f46; }
      `}} />

      {/* Sidebar */}
      <aside className="w-64 border-r border-zinc-800/60 bg-[#09090b] flex flex-col h-screen shrink-0">
        {/* Header */}
        <div className="h-14 px-4 flex items-center justify-between border-b border-zinc-800/60 shrink-0">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 bg-indigo-500 rounded-sm flex items-center justify-center">
              <Zap className="w-4 h-4 text-white" />
            </div>
            <span className="font-display font-semibold tracking-tight text-zinc-100">PicGenAI</span>
          </div>
        </div>

        {/* Global actions */}
        <div className="p-3 space-y-1">
          <button className="w-full flex items-center gap-2 px-3 py-2 text-sm text-zinc-300 hover:text-zinc-50 hover:bg-zinc-800/50 transition-colors">
            <Plus className="w-4 h-4" />
            <span>Новый чат</span>
            <kbd className="ml-auto font-mono text-[10px] text-zinc-500 tracking-tighter">⌘ N</kbd>
          </button>
          <button className="w-full flex items-center gap-2 px-3 py-2 text-sm text-zinc-400 hover:text-zinc-50 hover:bg-zinc-800/50 transition-colors">
            <Layers className="w-4 h-4" />
            <span>Лента работ</span>
          </button>
        </div>

        {/* Chat List */}
        <div className="flex-1 overflow-y-auto py-2">
          <div className="px-4 text-xs font-mono text-zinc-500 mb-2 uppercase tracking-wider">История</div>
          <div className="space-y-0.5 px-2">
            <ChatListItem title="Постеры для кофейни" mode="image" active />
            <ChatListItem title="Превью YouTube" mode="image" />
            <ChatListItem title="Лоу-фай биты" mode="music" />
            <ChatListItem title="Сцена для рилса" mode="video" />
            <ChatListItem title="Логотип студии" mode="image" />
            <ChatListItem title="Сценарий ролика" mode="chat" />
            <ChatListItem title="Джингл для интро" mode="music" />
          </div>
        </div>

        {/* Footer info */}
        <div className="mt-auto border-t border-zinc-800/60 p-3 bg-[#09090b]">
          <div className="flex items-center justify-between mb-3 px-1">
            <div className="flex flex-col">
              <span className="text-xs text-zinc-500 font-mono">Баланс</span>
              <span className="text-sm font-semibold text-zinc-200">1,250 <span className="text-zinc-500 font-normal">кр.</span></span>
            </div>
            <button className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs font-medium transition-colors">
              Пополнить
            </button>
          </div>
          <div className="flex items-center justify-between px-2 py-2 hover:bg-zinc-800/50 cursor-pointer transition-colors group">
            <div className="flex items-center gap-2 text-sm text-zinc-400 group-hover:text-zinc-200">
              <div className="w-6 h-6 bg-zinc-800 rounded-sm flex items-center justify-center font-mono text-[10px] text-zinc-300">
                MK
              </div>
              <span className="font-mono text-xs">@designer_mk</span>
            </div>
            <LogOut className="w-4 h-4 text-zinc-600 group-hover:text-zinc-400" />
          </div>
        </div>
      </aside>

      {/* Main Area */}
      <main className="flex-1 flex flex-col h-screen min-w-0 bg-[#0c0c0e]">
        {/* Header */}
        <header className="h-14 px-4 flex items-center justify-between border-b border-zinc-800/60 bg-[#09090b]/80 backdrop-blur-sm shrink-0">
          <div className="flex items-center gap-3">
            <Hash className="w-4 h-4 text-zinc-500" />
            <h1 className="text-sm font-medium text-zinc-200">Постеры для кофейни</h1>
            <span className="px-1.5 py-0.5 bg-zinc-800 text-zinc-400 text-[10px] font-mono tracking-wide">IMG-GEN</span>
          </div>
          <div className="flex items-center gap-1">
            <HeaderButton icon={Edit2} tooltip="Переименовать" />
            <HeaderButton icon={Archive} tooltip="В архив" />
            <HeaderButton icon={Trash2} tooltip="Удалить" />
            <div className="w-px h-4 bg-zinc-800 mx-1" />
            <HeaderButton icon={Settings} tooltip="Настройки" />
          </div>
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 md:p-6 lg:p-8 space-y-8 scroll-smooth">
          
          {/* User Message */}
          <div className="max-w-3xl mx-auto flex gap-4 w-full">
            <div className="w-8 h-8 rounded-sm bg-zinc-800 flex items-center justify-center shrink-0">
              <span className="font-mono text-[10px] text-zinc-400">MK</span>
            </div>
            <div className="flex-1 space-y-1">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-zinc-200">@designer_mk</span>
                <span className="text-[10px] font-mono text-zinc-600">14:22</span>
              </div>
              <p className="text-sm text-zinc-300 leading-relaxed">
                Сгенерируй постер для спешелти кофейни. Минимализм, типографика в швейцарском стиле, темные тона, зерна спешелти обжарки крупным планом, легкий пар. Соотношение 3:4.
              </p>
            </div>
          </div>

          {/* AI Text Response */}
          <div className="max-w-3xl mx-auto flex gap-4 w-full">
            <div className="w-8 h-8 rounded-sm bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center shrink-0">
              <Zap className="w-4 h-4 text-indigo-400" />
            </div>
            <div className="flex-1 space-y-2">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-indigo-300">PicGenAI</span>
                <span className="text-[10px] font-mono text-zinc-600">14:22</span>
                <span className="text-[10px] font-mono text-indigo-500/50 bg-indigo-500/10 px-1 py-0.5">Claude-3.5-Sonnet</span>
              </div>
              <p className="text-sm text-zinc-300 leading-relaxed">
                Принято. Я подготовлю промпт для генерации постера с фокусом на текстуру зерен и строгую верстку. Использую модель Midjourney v6 для наилучшей детализации.
              </p>
            </div>
          </div>

          {/* AI Image Response */}
          <div className="max-w-3xl mx-auto flex gap-4 w-full">
            <div className="w-8 h-8 rounded-sm bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center shrink-0">
              <Zap className="w-4 h-4 text-indigo-400" />
            </div>
            <div className="flex-1 space-y-2">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-indigo-300">PicGenAI</span>
                <span className="text-[10px] font-mono text-zinc-600">14:24</span>
                <span className="text-[10px] font-mono text-indigo-500/50 bg-indigo-500/10 px-1 py-0.5">Midjourney-v6</span>
                <span className="text-[10px] font-mono text-zinc-500 ml-auto">-25 кр.</span>
                <span className="text-[10px] font-mono text-zinc-600">12.4s</span>
              </div>
              
              <div className="relative group max-w-sm rounded-sm overflow-hidden border border-zinc-800 bg-zinc-900">
                <img 
                  src="https://images.unsplash.com/photo-1682687220742-aba13b6e50ba?auto=format&fit=crop&w=800&q=80" 
                  alt="Coffee Poster" 
                  className="w-full h-auto aspect-[3/4] object-cover transition-transform duration-700 group-hover:scale-[1.02]"
                />
                <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors opacity-0 group-hover:opacity-100 flex flex-col justify-end p-3">
                  <div className="flex gap-2">
                    <button className="flex-1 bg-white text-black text-xs font-medium py-1.5 rounded-sm hover:bg-zinc-200 transition-colors">Скачать</button>
                    <button className="w-8 h-8 bg-zinc-800 text-white rounded-sm flex items-center justify-center hover:bg-zinc-700 transition-colors"><MoreVertical className="w-4 h-4" /></button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* User Message - Request Music */}
          <div className="max-w-3xl mx-auto flex gap-4 w-full">
            <div className="w-8 h-8 rounded-sm bg-zinc-800 flex items-center justify-center shrink-0">
              <span className="font-mono text-[10px] text-zinc-400">MK</span>
            </div>
            <div className="flex-1 space-y-1">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-zinc-200">@designer_mk</span>
                <span className="text-[10px] font-mono text-zinc-600">14:30</span>
              </div>
              <p className="text-sm text-zinc-300 leading-relaxed">
                Отлично. Теперь нужен короткий фоновый трек для промо-сторис с этим постером. Lo-fi hip-hop, медленный ритм, виниловый треск, джазовые аккорды на пианино. 15 секунд.
              </p>
            </div>
          </div>

          {/* AI Music Response */}
          <div className="max-w-3xl mx-auto flex gap-4 w-full pb-8">
            <div className="w-8 h-8 rounded-sm bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center shrink-0">
              <Zap className="w-4 h-4 text-indigo-400" />
            </div>
            <div className="flex-1 space-y-2">
              <div className="flex items-baseline gap-2">
                <span className="text-sm font-medium text-indigo-300">PicGenAI</span>
                <span className="text-[10px] font-mono text-zinc-600">14:31</span>
                <span className="text-[10px] font-mono text-indigo-500/50 bg-indigo-500/10 px-1 py-0.5">Suno-v3.5</span>
                <span className="text-[10px] font-mono text-zinc-500 ml-auto">-10 кр.</span>
                <span className="text-[10px] font-mono text-zinc-600">45.2s</span>
              </div>
              
              <div className="max-w-md w-full border border-zinc-800 bg-[#09090b] rounded-sm p-3 flex items-center gap-3">
                <button className="w-10 h-10 bg-indigo-500 hover:bg-indigo-400 text-white rounded-sm flex items-center justify-center shrink-0 transition-colors">
                  <Play className="w-4 h-4 ml-0.5" />
                </button>
                <div className="flex-1 min-w-0 flex flex-col gap-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-zinc-200 truncate">Coffee_Shop_Lofi.wav</span>
                    <span className="text-[10px] font-mono text-zinc-500 shrink-0">0:15</span>
                  </div>
                  {/* Fake waveform */}
                  <div className="flex items-end gap-[2px] h-4 w-full">
                    {[...Array(40)].map((_, i) => (
                      <div 
                        key={i} 
                        className="w-full bg-zinc-700/50 rounded-t-sm" 
                        style={{ height: `${Math.max(20, Math.random() * 100)}%` }}
                      />
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Composer */}
        <div className="p-4 border-t border-zinc-800/60 bg-[#09090b] shrink-0">
          <div className="max-w-3xl mx-auto flex flex-col gap-2">
            
            {/* Context/Mode Toolbar */}
            <div className="flex flex-wrap items-center gap-2 mb-1">
              <div className="flex p-0.5 bg-zinc-900 border border-zinc-800 rounded-sm">
                <ModeButton icon={MessageSquare} label="Чат" />
                <ModeButton icon={ImageIcon} label="Изображение" active />
                <ModeButton icon={Video} label="Видео" />
                <ModeButton icon={Music} label="Музыка" />
              </div>
              
              <div className="w-px h-4 bg-zinc-800 mx-1 hidden sm:block" />
              
              <button className="flex items-center gap-1.5 px-2 py-1 bg-zinc-900 border border-zinc-800 hover:border-zinc-600 rounded-sm text-xs font-mono text-zinc-400 transition-colors">
                Midjourney-v6 <ChevronDown className="w-3 h-3" />
              </button>
              
              <button className="flex items-center gap-1.5 px-2 py-1 bg-zinc-900 border border-zinc-800 hover:border-zinc-600 rounded-sm text-xs font-mono text-zinc-400 transition-colors">
                <SlidersHorizontal className="w-3 h-3" /> 3:4
              </button>
              
              <div className="ml-auto flex items-center gap-2 text-[10px] font-mono text-zinc-500">
                <Globe className="w-3 h-3" />
                <span>Веб-поиск</span>
                <div className="w-6 h-3 bg-indigo-500/20 rounded-full flex items-center p-[1px]">
                  <div className="w-2.5 h-2.5 bg-indigo-500 rounded-full ml-auto" />
                </div>
              </div>
            </div>

            {/* Input Area */}
            <div className="relative flex items-end gap-2 bg-zinc-900 border border-zinc-800 focus-within:border-zinc-600 rounded-sm p-1 transition-colors">
              <button className="p-2 text-zinc-500 hover:text-zinc-300 transition-colors shrink-0">
                <Paperclip className="w-5 h-5" />
              </button>
              <textarea 
                className="flex-1 max-h-32 min-h-[44px] bg-transparent border-none focus:ring-0 resize-none py-2.5 text-sm text-zinc-200 placeholder-zinc-600 font-sans"
                placeholder="Опишите, что сгенерировать…"
                defaultValue=""
                rows={1}
              />
              <div className="flex flex-col items-center gap-1 p-1 shrink-0">
                <span className="text-[10px] font-mono text-zinc-500 mb-1">-25 кр.</span>
                <button className="w-8 h-8 bg-zinc-100 hover:bg-white text-zinc-900 rounded-sm flex items-center justify-center transition-colors">
                  <Send className="w-4 h-4 ml-0.5" />
                </button>
              </div>
            </div>
            
            <div className="text-center mt-1">
              <span className="text-[10px] font-mono text-zinc-600">Возможности модели: текст→изображение, вариации, увеличение. Enter — отправить.</span>
            </div>

          </div>
        </div>
      </main>
    </div>
  );
}

function ChatListItem({ title, mode, active = false }: { title: string, mode: "chat" | "image" | "video" | "music", active?: boolean }) {
  const Icon = {
    chat: MessageSquare,
    image: ImageIcon,
    video: Video,
    music: Music
  }[mode];

  return (
    <button className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-sm transition-colors text-left
      ${active ? "bg-zinc-800/80 text-zinc-100 border border-zinc-700/50" : "text-zinc-400 hover:bg-zinc-900 border border-transparent"}
    `}>
      <Icon className={`w-3.5 h-3.5 shrink-0 ${active ? "text-indigo-400" : "text-zinc-500"}`} />
      <span className="truncate flex-1">{title}</span>
    </button>
  );
}

function HeaderButton({ icon: Icon, tooltip }: { icon: any, tooltip: string }) {
  return (
    <button className="w-8 h-8 flex items-center justify-center text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 rounded-sm transition-colors" title={tooltip}>
      <Icon className="w-4 h-4" />
    </button>
  );
}

function ModeButton({ icon: Icon, label, active = false }: { icon: any, label: string, active?: boolean }) {
  return (
    <button className={`flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-sm transition-colors
      ${active ? "bg-zinc-800 text-zinc-200" : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50"}
    `}>
      <Icon className="w-3.5 h-3.5" />
      <span>{label}</span>
    </button>
  );
}
