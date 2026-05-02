import React, { useEffect } from "react";
import { 
  Play, 
  Pause, 
  Image as ImageIcon, 
  Video, 
  Music, 
  MessageSquare, 
  MoreHorizontal, 
  Settings, 
  Archive, 
  Trash2, 
  Edit3, 
  Paperclip, 
  Send, 
  Plus, 
  Grid, 
  Wallet,
  LogOut,
  ChevronDown,
  Wand2,
  Volume2,
  Globe
} from "lucide-react";

export function Editorial() {
  useEffect(() => {
    const link = document.createElement("link");
    link.href = "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&family=Syne:wght@500;600;700;800&display=swap";
    link.rel = "stylesheet";
    document.head.appendChild(link);
    return () => {
      document.head.removeChild(link);
    };
  }, []);

  return (
    <div 
      className="min-h-screen flex text-[#e4e4e7] selection:bg-[#9b8afb] selection:text-white"
      style={{ 
        backgroundColor: "#09090b",
        fontFamily: "'Inter', sans-serif" 
      }}
    >
      {/* SIDEBAR */}
      <div className="w-[320px] flex flex-col border-r border-[#27272a] bg-[#09090b] flex-shrink-0">
        
        <div className="p-8 pb-6">
          <h1 
            className="text-3xl font-extrabold tracking-tight text-white flex items-center gap-3"
            style={{ fontFamily: "'Syne', sans-serif" }}
          >
            <Wand2 className="w-7 h-7 text-[#9b8afb]" />
            PicGenAI
          </h1>
        </div>

        <div className="px-6 mb-8">
          <button className="w-full bg-white text-black py-4 px-6 rounded-none font-bold tracking-wide flex items-center justify-between hover:bg-[#9b8afb] hover:text-white transition-colors">
            <span>НОВЫЙ ЧАТ</span>
            <Plus className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 mb-8">
          <button className="w-full flex items-center gap-4 py-3 px-4 text-[#a1a1aa] hover:text-white transition-colors border border-[#27272a] hover:border-[#9b8afb]">
            <Grid className="w-5 h-5" />
            <span className="font-semibold uppercase tracking-wider text-sm">Лента работ</span>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6">
          <h2 className="text-xs uppercase tracking-widest text-[#52525b] mb-4 font-semibold">История</h2>
          
          <div className="space-y-1">
            <ChatItem title="Постеры для кофейни" type="image" active />
            <ChatItem title="Превью YouTube" type="image" />
            <ChatItem title="Лоу-фай биты" type="music" />
            <ChatItem title="Сцена для рилса" type="video" />
            <ChatItem title="Логотип студии" type="image" />
            <ChatItem title="Сценарий ролика" type="text" />
          </div>
        </div>

        <div className="p-6 border-t border-[#27272a] bg-[#0c0c0e]">
          <div className="flex items-center justify-between mb-6 bg-[#18181b] p-4 border border-[#27272a]">
            <div>
              <div className="text-[10px] uppercase tracking-widest text-[#a1a1aa] mb-1">Баланс</div>
              <div className="font-mono text-lg font-bold text-white">1,240 <span className="text-[#9b8afb] text-sm">CR</span></div>
            </div>
            <button className="bg-[#27272a] hover:bg-[#9b8afb] text-white p-2 transition-colors">
              <Wallet className="w-4 h-4" />
            </button>
          </div>

          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-[#27272a] flex items-center justify-center font-bold font-mono text-[#9b8afb]">
                A
              </div>
              <div>
                <div className="text-sm font-bold text-white">@artdirector</div>
                <div className="text-xs text-[#a1a1aa]">Pro Plan</div>
              </div>
            </div>
            <button className="text-[#52525b] hover:text-white transition-colors">
              <LogOut className="w-5 h-5" />
            </button>
          </div>
        </div>
      </div>

      {/* MAIN AREA */}
      <div className="flex-1 flex flex-col min-w-0 bg-[#09090b] relative">
        
        {/* Header */}
        <header className="h-[100px] flex items-center justify-between px-10 border-b border-[#27272a]">
          <h2 
            className="text-4xl font-bold text-white"
            style={{ fontFamily: "'Syne', sans-serif" }}
          >
            Постеры для кофейни
          </h2>
          
          <div className="flex items-center gap-3">
            <IconButton icon={<Edit3 />} label="Переименовать" />
            <IconButton icon={<Archive />} label="Архив" />
            <IconButton icon={<Settings />} label="Настройки" />
            <div className="w-px h-6 bg-[#27272a] mx-2"></div>
            <IconButton icon={<Trash2 />} label="Удалить" danger />
          </div>
        </header>

        {/* Chat History */}
        <div className="flex-1 overflow-y-auto p-10 pb-40 space-y-12 max-w-4xl mx-auto w-full">
          
          <UserMessage text="Сгенерируй атмосферный постер для спешелти кофейни. Темный фон, пар над чашкой, кинематографичный свет. Текст не нужен, только визуал." />
          
          <AiMessage>
            <p className="text-lg leading-relaxed text-[#d4d4d8] mb-6">
              Я подготовил кинематографичный постер с акцентом на текстуру кофе и игру света. Использована модель <span className="font-mono text-[#9b8afb] text-sm bg-[#9b8afb]/10 px-2 py-1">Midjourney v6</span> с соотношением сторон 16:9.
            </p>
            <div className="border border-[#27272a] p-2 bg-[#0c0c0e]">
              <img 
                src="/__mockup/images/coffee-poster.png" 
                alt="Постер кофейни" 
                className="w-full h-auto object-cover opacity-90 hover:opacity-100 transition-opacity"
              />
              <div className="flex items-center justify-between mt-3 px-2 pb-1">
                <span className="font-mono text-xs text-[#a1a1aa]">1920x1080 • 4.2 MB</span>
                <button className="text-xs uppercase tracking-widest font-bold text-[#9b8afb] hover:text-white transition-colors">Скачать</button>
              </div>
            </div>
          </AiMessage>

          <UserMessage text="Отлично. Теперь добавь фоновый трек для рилса с этим постером. Мягкий лоу-фай хип-хоп, медленный темп, пианино и виниловый треск." />

          <AiMessage>
            <p className="text-lg leading-relaxed text-[#d4d4d8] mb-6">
              Сгенерирован лоу-фай трек длительностью 30 секунд. Инструменты: пианино, джазовые барабаны, виниловый шум.
            </p>
            <div className="border border-[#27272a] bg-[#18181b] p-6 flex items-center gap-6">
              <button className="w-14 h-14 bg-white text-black flex items-center justify-center hover:bg-[#9b8afb] hover:text-white transition-colors shrink-0">
                <Play className="w-6 h-6 ml-1" />
              </button>
              <div className="flex-1">
                <div className="flex justify-between items-end mb-3">
                  <span className="font-bold text-white text-lg">Midnight Brew (Lo-Fi)</span>
                  <span className="font-mono text-sm text-[#9b8afb]">00:30</span>
                </div>
                {/* Abstract waveform */}
                <div className="flex items-center gap-1 h-8">
                  {[...Array(40)].map((_, i) => (
                    <div 
                      key={i} 
                      className="flex-1 bg-[#3f3f46] rounded-full"
                      style={{ 
                        height: `${Math.max(10, Math.sin(i * 0.4) * 50 + Math.random() * 50)}%`,
                        backgroundColor: i < 12 ? '#9b8afb' : '#3f3f46'
                      }}
                    ></div>
                  ))}
                </div>
              </div>
            </div>
          </AiMessage>

        </div>

        {/* Composer */}
        <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-[#09090b] via-[#09090b] to-transparent pt-20 pb-8 px-10">
          <div className="max-w-4xl mx-auto">
            
            {/* Context parameters row */}
            <div className="flex flex-wrap items-center gap-4 mb-4">
              
              <div className="flex bg-[#18181b] border border-[#27272a] p-1">
                <ModeButton icon={<MessageSquare />} label="Чат" />
                <ModeButton icon={<ImageIcon />} label="Изображение" active />
                <ModeButton icon={<Video />} label="Видео" />
                <ModeButton icon={<Music />} label="Музыка" />
              </div>

              <div className="flex items-center gap-2 bg-[#18181b] border border-[#27272a] px-3 py-2 cursor-pointer hover:border-[#9b8afb] transition-colors">
                <span className="font-mono text-xs text-[#a1a1aa]">Модель</span>
                <span className="font-bold text-sm text-white">Midjourney v6</span>
                <ChevronDown className="w-4 h-4 text-[#a1a1aa]" />
              </div>

              <div className="flex items-center gap-2 bg-[#18181b] border border-[#27272a] px-3 py-2 cursor-pointer hover:border-[#9b8afb] transition-colors">
                <span className="font-mono text-xs text-[#a1a1aa]">AR</span>
                <span className="font-bold text-sm text-white">16:9</span>
              </div>

              <div className="flex items-center gap-3 bg-[#18181b] border border-[#27272a] px-3 py-2">
                <label className="flex items-center gap-2 cursor-pointer text-sm font-medium text-[#a1a1aa] hover:text-white transition-colors">
                  <input type="checkbox" className="accent-[#9b8afb] bg-transparent border-[#52525b]" />
                  <Globe className="w-4 h-4" /> Поиск
                </label>
              </div>

            </div>

            <div className="relative flex items-end border border-[#52525b] focus-within:border-[#9b8afb] bg-[#0c0c0e] transition-colors">
              <button className="p-4 text-[#a1a1aa] hover:text-white transition-colors shrink-0">
                <Paperclip className="w-6 h-6" />
              </button>
              
              <textarea 
                className="w-full bg-transparent border-none outline-none resize-none py-5 px-2 text-lg text-white placeholder:text-[#52525b] font-medium leading-relaxed min-h-[60px] max-h-[200px]"
                placeholder="Опишите, что сгенерировать..."
                rows={1}
                defaultValue="Создай еще один вариант постера, но теперь с видом сверху на чашку."
              ></textarea>
              
              <div className="p-3 shrink-0 flex items-center gap-4">
                <div className="font-mono text-xs text-[#a1a1aa] text-right">
                  <span className="text-[#9b8afb] font-bold">-4</span> CR
                </div>
                <button className="w-12 h-12 bg-white text-black flex items-center justify-center hover:bg-[#9b8afb] hover:text-white transition-colors">
                  <Send className="w-5 h-5 ml-1" />
                </button>
              </div>
            </div>
            
          </div>
        </div>

      </div>
    </div>
  );
}

function ChatItem({ title, type, active = false }: { title: string, type: 'text' | 'image' | 'video' | 'music', active?: boolean }) {
  const icons = {
    text: <MessageSquare className="w-4 h-4" />,
    image: <ImageIcon className="w-4 h-4" />,
    video: <Video className="w-4 h-4" />,
    music: <Music className="w-4 h-4" />
  };

  return (
    <button className={`w-full flex items-center gap-3 py-3 px-4 text-left border-l-2 transition-all ${active ? 'border-[#9b8afb] bg-[#18181b] text-white' : 'border-transparent text-[#a1a1aa] hover:bg-[#18181b] hover:text-white'}`}>
      <span className={active ? 'text-[#9b8afb]' : 'text-[#52525b]'}>
        {icons[type]}
      </span>
      <span className="font-medium text-sm truncate">{title}</span>
    </button>
  );
}

function IconButton({ icon, label, danger = false }: { icon: React.ReactNode, label: string, danger?: boolean }) {
  return (
    <button 
      className={`p-3 border border-[#27272a] hover:border-current transition-colors ${danger ? 'text-[#ef4444] hover:bg-[#ef4444]/10' : 'text-[#a1a1aa] hover:text-white hover:bg-[#27272a]'}`}
      title={label}
    >
      {React.cloneElement(icon as React.ReactElement, { className: 'w-5 h-5' })}
    </button>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] pl-10 border-l border-[#27272a]">
        <p className="text-xl font-medium text-white leading-relaxed" style={{ fontFamily: "'Syne', sans-serif" }}>
          {text}
        </p>
      </div>
    </div>
  );
}

function AiMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-6">
      <div className="w-10 h-10 bg-[#9b8afb] flex items-center justify-center shrink-0">
        <Wand2 className="w-5 h-5 text-white" />
      </div>
      <div className="flex-1 min-w-0 pt-1">
        {children}
      </div>
    </div>
  );
}

function ModeButton({ icon, label, active = false }: { icon: React.ReactNode, label: string, active?: boolean }) {
  return (
    <button 
      className={`px-4 py-2 flex items-center gap-2 text-sm font-bold tracking-wide transition-colors ${active ? 'bg-white text-black' : 'text-[#a1a1aa] hover:text-white hover:bg-[#27272a]'}`}
    >
      {React.cloneElement(icon as React.ReactElement, { className: 'w-4 h-4' })}
      <span className="uppercase text-[10px]">{label}</span>
    </button>
  );
}
