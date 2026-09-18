#include "recording/native_capture.hpp"
#include <windows.h>
#include <iostream>
#include <stdexcept>
using namespace lvz::recording;
void Check(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
template<class Fn>void Reject(Fn fn){try{fn();}catch(const std::exception&){return;}throw std::runtime_error("bad pixels accepted");}
int main(){try{
    // RGB565: red, green, blue, white. Validate actual scaling and negative pitch.
    std::vector<uint8_t> packed={0x00,0xf8,0xe0,0x07,0xaa,0xaa,0x1f,0x00,0xff,0xff,0xaa,0xaa};
    PixelLayout p{2,2,16,6,0xf800,0x07e0,0x001f};
    Check(ConvertPixels(packed,p)==std::vector<uint8_t>({0,0,255,0,255,0,255,0,0,255,255,255}),"565 conversion");
    p.pitch=-6;
    Check(ConvertPixels(packed,p)==std::vector<uint8_t>({255,0,0,255,255,255,0,0,255,0,255,0}),"negative pitch orientation");
    p={2,1,32,8,0x00ff0000,0x0000ff00,0x000000ff};
    Check(ConvertPixels(std::vector<uint8_t>{3,2,1,0xff,6,5,4,0},p)==std::vector<uint8_t>({3,2,1,6,5,4}),"XRGB8888 conversion");
    p={2,1,24,6,0x000000ff,0x0000ff00,0x00ff0000};
    Check(ConvertPixels(std::vector<uint8_t>{1,2,3,4,5,6},p)==std::vector<uint8_t>({3,2,1,6,5,4}),"RGB24 channel masks");
    Reject([&]{ConvertPixels(std::vector<uint8_t>{1,2},p);});
    p.greenMask=p.redMask;Reject([&]{ConvertPixels(std::vector<uint8_t>(6),p);});
    p.width=0;Reject([&]{ConvertPixels(std::vector<uint8_t>(6),p);});
    Check(!ValidateCaptureTarget(),"test executable unexpectedly identified as PvZ");
    auto unsupported=CaptureOriginalFrame(GetCurrentThreadId());Check(!unsupported.ok&&!unsupported.error.empty(),"unsupported target not rejected");
    auto wrongThread=CaptureOriginalFrame(0);Check(!wrongThread.ok&&wrongThread.error.find("thread")!=std::string::npos,"thread guard missing");
    std::cout<<"native capture pixel conversion and target/thread guards passed\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
