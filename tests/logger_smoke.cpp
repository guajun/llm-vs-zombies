#include "buffered_writer.hpp"
#include <iostream>

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    lvz::BufferedWriter writer;
    writer.Open(argv[1]);
    for (int i=0; i<3000; ++i) {
        writer.Append("{\"seq\":"+std::to_string(i)+",\"message\":"+lvz::Quote("quote\" newline\n 中文")+"}");
    }
    writer.Close();
    std::cout << "3000 buffered records written\n";
}
