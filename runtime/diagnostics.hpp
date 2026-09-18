#pragma once
#include <avz_logger.h>
#include <filesystem>
#include <fstream>
namespace lvz::runtime {
inline std::ofstream diagnosticStream;
inline void SetDiagnosticsPath(const std::filesystem::path& path) {
    diagnosticStream.open(path,std::ios::binary|std::ios::app);
}
class DiagnosticLogger final:public AAbstractLogger {
protected:
    void _Output(ALogLevel,std::string&& message) override {
        OutputDebugStringA(message.c_str());
        if(diagnosticStream.is_open()) { diagnosticStream<<message;diagnosticStream.flush(); }
    }
};
}
