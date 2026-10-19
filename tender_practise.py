"""兼容原运行命令的入口；具体代码在tender_app中。"""
from tender_app.app import run_configured_sites

if __name__ == "__main__":
    run_configured_sites()

