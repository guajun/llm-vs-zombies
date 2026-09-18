#include "native_capture.hpp"
#include <windows.h>
#include <ddraw.h>
#include <d3d.h>
#include <algorithm>
#include <array>
#include <bit>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace lvz::recording {
namespace {
constexpr uintptr_t AppPointer=0x6a9ec0;
constexpr uintptr_t GlobalMt=0x75a910;
constexpr size_t MtBytes=625*4; // 624 words AND cursor.
bool Accessible(uintptr_t address,size_t length,bool writable=false) noexcept {
    if(!address||length>std::numeric_limits<uintptr_t>::max()-address) return false;
    const uintptr_t end=address+length;
    while(address<end) {
        MEMORY_BASIC_INFORMATION info{};
        if(!VirtualQuery(reinterpret_cast<void*>(address),&info,sizeof(info))||info.State!=MEM_COMMIT||
           (info.Protect&(PAGE_NOACCESS|PAGE_GUARD))) return false;
        if(writable&&!(info.Protect&(PAGE_READWRITE|PAGE_WRITECOPY|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY))) return false;
        uintptr_t regionEnd=reinterpret_cast<uintptr_t>(info.BaseAddress)+info.RegionSize;
        if(regionEnd<=address) return false;
        address=regionEnd;
    }
    return true;
}
template<class T> T Read(uintptr_t address) {
    if(!Accessible(address,sizeof(T))) throw std::runtime_error("capture encountered an unreadable engine field");
    T value;std::memcpy(&value,reinterpret_cast<void*>(address),sizeof(value));return value;
}
template<size_t N> bool Match(uintptr_t address,const uint8_t (&expected)[N]) noexcept {
    return Accessible(address,N)&&!std::memcmp(reinterpret_cast<void*>(address),expected,N);
}
struct Mask {
    uint32_t bits;unsigned shift;uint32_t maximum;
    explicit Mask(uint32_t value):bits(value),shift(value?std::countr_zero(value):0),maximum(value>>shift) {
        if(!value||(maximum&(maximum+1u))) throw std::runtime_error("surface RGB masks must be nonzero and contiguous");
    }
    uint8_t Extract(uint32_t value)const{return uint8_t((uint64_t((value&bits)>>shift)*255+maximum/2)/maximum);}
};
struct DirtyRestore {
    std::vector<std::pair<uintptr_t,uint8_t>> saved;
    ~DirtyRestore() { for(auto [address,value]:saved) if(Accessible(address,1,true)) *reinterpret_cast<uint8_t*>(address)=value; }
    void Mark(uintptr_t manager) {
        std::vector<uintptr_t> remaining{manager};std::unordered_set<uintptr_t> seen;
        while(!remaining.empty()) {
            auto widget=remaining.back();remaining.pop_back();
            if(!seen.insert(widget).second) throw std::runtime_error("widget graph contains a cycle");
            if(seen.size()>1024||!Accessible(widget,0x55)||!Accessible(widget+0x2c,1,true)) throw std::runtime_error("unsupported widget layout");
            saved.emplace_back(widget+0x2c,Read<uint8_t>(widget+0x2c));
            *reinterpret_cast<uint8_t*>(widget+0x2c)=1;
            // VC2005 list layout verified at 0x538eef: manager+8 is head,
            // head->next at +0, nodes hold Widget* at +8. Do not use our STL ABI.
            auto head=Read<uintptr_t>(widget+8);auto node=Read<uintptr_t>(head);
            size_t count=0;
            while(node!=head) {
                if(++count>1024) throw std::runtime_error("widget list does not terminate");
                remaining.push_back(Read<uintptr_t>(node+8));node=Read<uintptr_t>(node);
            }
        }
    }
};
void Flush3D(uintptr_t d3d) {
#if defined(__i386__)
    // 0x563011 sets ESI, then calls this function; it ends the pending D3D scene.
    asm volatile("call *%1" : "+S"(d3d) : "r"(uintptr_t(0x568ec0)) : "eax","ecx","edx","memory","cc");
#else
    (void)d3d;throw std::runtime_error("capture adapter requires i386");
#endif
}
struct LockGuard {
    IDirectDrawSurface* surface;
    bool locked=false;
    ~LockGuard(){if(locked) surface->Unlock(nullptr);}
};
struct DrawingGuard {
    uintptr_t address;
    explicit DrawingGuard(uintptr_t app):address(app+0x458) {
        if(!Accessible(address,1,true)||Read<uint8_t>(address)) throw std::runtime_error("capture cannot re-enter an active engine draw");
        *reinterpret_cast<uint8_t*>(address)=1;
    }
    ~DrawingGuard(){*reinterpret_cast<uint8_t*>(address)=0;}
};
struct KnownRandomState {
    std::array<uint8_t,MtBytes> mt{};
    uintptr_t crtAddress=0;uint32_t crt=0;
    KnownRandomState() {
        if(!Accessible(GlobalMt,mt.size())) throw std::runtime_error("MT state unavailable before capture");
        std::memcpy(mt.data(),reinterpret_cast<void*>(GlobalMt),mt.size());
        using GetPtd=void* (__cdecl*)();
        crtAddress=reinterpret_cast<uintptr_t>(reinterpret_cast<GetPtd>(0x628a3d)())+0x14;
        crt=Read<uint32_t>(crtAddress);
    }
    bool Unchanged()const {
        return !std::memcmp(mt.data(),reinterpret_cast<void*>(GlobalMt),mt.size())&&crt==Read<uint32_t>(crtAddress);
    }
};
}

std::vector<uint8_t> ConvertPixels(std::span<const uint8_t> memory,const PixelLayout& p) {
    if(!p.width||!p.height||p.width>4096||p.height>4096||(p.bitsPerPixel!=16&&p.bitsPerPixel!=24&&p.bitsPerPixel!=32)||
       !p.pitch||p.pitch==std::numeric_limits<int32_t>::min()) throw std::runtime_error("unsupported surface dimensions or pixel format");
    size_t bytes=p.bitsPerPixel/8,stride=static_cast<size_t>(p.pitch<0?-p.pitch:p.pitch);
    if(stride<size_t(p.width)*bytes||stride*size_t(p.height)>memory.size()) throw std::runtime_error("surface pitch/buffer length mismatch");
    Mask red(p.redMask),green(p.greenMask),blue(p.blueMask);
    if((red.bits&green.bits)||(red.bits&blue.bits)||(green.bits&blue.bits)||
       (p.bitsPerPixel<32&&((red.bits|green.bits|blue.bits)>>p.bitsPerPixel))) throw std::runtime_error("overlapping or out-of-range RGB masks");
    std::vector<uint8_t> out(size_t(p.width)*p.height*3);
    for(size_t y=0;y<p.height;++y) {
        size_t sourceRow=p.pitch<0?p.height-1-y:y;
        const auto* row=memory.data()+sourceRow*stride;
        for(size_t x=0;x<p.width;++x) {
            uint32_t value=0;std::memcpy(&value,row+x*bytes,bytes);
            auto* pixel=out.data()+(y*p.width+x)*3;
            pixel[0]=blue.Extract(value);pixel[1]=green.Extract(value);pixel[2]=red.Extract(value);
        }
    }
    return out;
}

bool ValidateCaptureTarget() noexcept {
    if(sizeof(void*)!=4||reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr))!=0x400000) return false;
    if(!Accessible(0x400000,sizeof(IMAGE_DOS_HEADER))) return false;
    const auto* dos=reinterpret_cast<IMAGE_DOS_HEADER*>(0x400000);
    if(dos->e_magic!=IMAGE_DOS_SIGNATURE||dos->e_lfanew<0||dos->e_lfanew>0x1000||
       !Accessible(0x400000+dos->e_lfanew,sizeof(IMAGE_NT_HEADERS32))) return false;
    const auto* nt=reinterpret_cast<IMAGE_NT_HEADERS32*>(0x400000+dos->e_lfanew);
    if(nt->Signature!=IMAGE_NT_SIGNATURE||nt->FileHeader.Machine!=IMAGE_FILE_MACHINE_I386||
       nt->OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR32_MAGIC) return false;
    static constexpr uint8_t draw[]={0x6a,0xff,0x68,0xee,0x78,0x64,0x00,0x64,0xa1,0,0,0,0,0x50,0x81,0xec,0x68,0x01,0,0};
    static constexpr uint8_t manager[]={0x8b,0x8e,0x20,0x03,0,0,0x51,0xc6,0x86,0x58,0x04,0,0,0x01,0xe8,0x52,0xc7,0xfe,0xff};
    static constexpr uint8_t image[]={0x8b,0x4d,0x60,0xe8,0xce,0xda,0x04,0};
    static constexpr uint8_t surface[]={0x8b,0x47,0x68,0x53,0x6a,0x11,0x8d,0x94,0x24,0x04,0x01,0,0};
    static constexpr uint8_t flush[]={0x80,0x7e,0x3c,0,0x74,0x1d,0x8b,0x46,0x20,0x8b,0x08,0x8b,0x51,0x18,0x50,0xff,0xd2};
    static constexpr uint8_t crt[]={0xe8,0xb1,0xa9,0,0,0x8b,0x48,0x14,0x69,0xc9,0xfd,0x43,0x03,0};
    static constexpr uint8_t videoOnly[]={0x80,0xbf,0xfc,0x0c,0,0,0,0x75,0x45,0x8b,0x47,0x64};
    return Match(0x538eb0,draw)&&Match(0x54c74b,manager)&&Match(0x538f5a,image)&&
        Match(0x5630ed,surface)&&Match(0x568ec0,flush)&&Match(0x61e087,crt)&&Match(0x56354c,videoOnly);
}

CaptureResult CaptureOriginalFrame(uint32_t gameThreadId) {
    CaptureResult result;
    try {
        if(!gameThreadId||GetCurrentThreadId()!=gameThreadId) throw std::runtime_error("capture must run on the established game thread");
        if(!ValidateCaptureTarget()) throw std::runtime_error("capture target image/signature mismatch");
        auto app=Read<uintptr_t>(AppPointer);
        auto board=Read<uintptr_t>(app+0x768);
        if(!board||Read<int>(app+0x7fc)!=3) throw std::runtime_error("capture requires an active fight at a stable boundary");
        result.gameClockBefore=Read<int>(board+0x5568);
        auto manager=Read<uintptr_t>(app+0x320);
        auto dd=Read<uintptr_t>(app+0x36c);
        if(!manager||!dd||!Read<uint8_t>(dd+0xcdc)) throw std::runtime_error("engine rendering is not initialized");
        // Video-only mode draws into a different secondary target. Do not return
        // an unrelated old mDrawSurface image under the current-tick stamp.
        if(Read<uint8_t>(dd+0xcfc)) throw std::runtime_error("video-only secondary target is unsupported; use the standard engine draw surface");
        auto screenImage=Read<uintptr_t>(dd+0xce8);
        if(!screenImage||Read<uintptr_t>(manager+0x60)!=screenImage) throw std::runtime_error("widget target is not the engine screen image");
        auto* surface=reinterpret_cast<IDirectDrawSurface*>(Read<uintptr_t>(dd+0x68));
        if(!surface||!Accessible(reinterpret_cast<uintptr_t>(surface),sizeof(void*))) throw std::runtime_error("engine draw surface is unavailable");
        result.used3D=Read<uint8_t>(dd+0x38)!=0;
        uintptr_t d3d=0;
        if(result.used3D) {
            d3d=Read<uintptr_t>(dd+0x30);
            if(!d3d||!Read<uintptr_t>(d3d+0x20)) throw std::runtime_error("3D renderer is incomplete");
        }
        KnownRandomState randomBefore;
        DirtyRestore dirty;dirty.Mark(manager);
        if(result.used3D) {
            auto* device=reinterpret_cast<IDirect3DDevice7*>(Read<uintptr_t>(d3d+0x20));
            auto hr=device->Clear(0,nullptr,D3DCLEAR_TARGET|D3DCLEAR_ZBUFFER,0xff000000,0.0f,0);
            if(FAILED(hr)) hr=device->Clear(0,nullptr,D3DCLEAR_TARGET,0xff000000,0.0f,0);
            if(FAILED(hr)) throw std::runtime_error("could not clear the engine's 3D draw target");
        }
        // Proven caller at 0x54c74b pushes manager; callee ends with ret 4.
        using DrawScreen=bool (__stdcall*)(void*);
        bool drew=false;
        {DrawingGuard drawing(app);
            result.forcedRender=true;
            drew=reinterpret_cast<DrawScreen>(0x538eb0)(reinterpret_cast<void*>(manager));}
        if(result.used3D) Flush3D(d3d);
        result.gameClockAfter=Read<int>(board+0x5568);
        result.knownRngUnchanged=randomBefore.Unchanged();
        if(result.gameClockAfter!=result.gameClockBefore||!result.knownRngUnchanged)
            throw std::runtime_error("capture rendering changed the game clock or a monitored RNG; pixels rejected");
        if(!drew) throw std::runtime_error("engine did not draw current widgets; refusing stale frame");
        DDSURFACEDESC desc{};desc.dwSize=sizeof(desc);
        LockGuard locked{surface};HRESULT status=DDERR_WASSTILLDRAWING;
        for(int attempt=0;attempt<64;++attempt) {
            status=surface->Lock(nullptr,&desc,DDLOCK_READONLY|DDLOCK_DONOTWAIT,nullptr);
            if(status!=DDERR_WASSTILLDRAWING) break;
            Sleep(1);
        }
        if(FAILED(status)) throw std::runtime_error("DirectDraw surface lock failed: "+std::to_string(static_cast<uint32_t>(status)));
        locked.locked=true;
        if(!(desc.ddpfPixelFormat.dwFlags&DDPF_RGB)||!desc.lpSurface) throw std::runtime_error("draw surface is not readable RGB");
        PixelLayout layout{desc.dwWidth,desc.dwHeight,desc.ddpfPixelFormat.dwRGBBitCount,desc.lPitch,
            desc.ddpfPixelFormat.dwRBitMask,desc.ddpfPixelFormat.dwGBitMask,desc.ddpfPixelFormat.dwBBitMask};
        if(layout.width!=800||layout.height!=600) throw std::runtime_error("expected the engine's 800x600 logical draw surface");
        if(layout.pitch==std::numeric_limits<int32_t>::min()) throw std::runtime_error("invalid negative surface pitch");
        auto stride=size_t(layout.pitch<0?-layout.pitch:layout.pitch);
        if(!stride||stride>65536) throw std::runtime_error("surface pitch exceeds supported bounds");
        auto* lowest=static_cast<uint8_t*>(desc.lpSurface);
        if(layout.pitch<0) lowest+=int64_t(layout.pitch)*(layout.height-1);
        result.pixels=ConvertPixels({lowest,stride*layout.height},layout);
        if(std::none_of(result.pixels.begin(),result.pixels.end(),[](uint8_t c){return c!=0;})) throw std::runtime_error("draw surface is entirely black; capture unavailable");
        result.width=layout.width;result.height=layout.height;result.rowStride=layout.width*3;result.ok=true;
    } catch(const std::exception& error) {result.ok=false;result.error=error.what();result.pixels.clear();}
    return result;
}
}
