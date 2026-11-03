//+------------------------------------------------------------------+
//| YonexisBridge.mq5 - executes YONEXIS orders inside MetaTrader 5  |
//|                                                                  |
//| Install: MetaEditor > File > Open Data Folder > MQL5 > Experts,  |
//| copy this file there, open it in MetaEditor and press Compile.   |
//| Then drag "YonexisBridge" onto ANY one chart, tick "Allow Algo   |
//| Trading", and switch on the Algo Trading button in the toolbar.  |
//|                                                                  |
//| Transport "file" (YONEXIS on the same Mac): commands arrive as   |
//| files in <Common Files>\YONEXIS\in, results go to \out, the      |
//| account / positions / quotes snapshot to \state.json.            |
//| Transport "web": the EA calls https://<dashboard>/api/mt5/bridge |
//| (add that address in Tools > Options > Expert Advisors > "Allow  |
//| WebRequest for listed URL").                                     |
//|                                                                  |
//| The EA only executes what YONEXIS sends; it never decides trades.|
//+------------------------------------------------------------------+
#property copyright "YONEXIS"
#property version   "1.14"

#include <Trade\Trade.mqh>

enum ENUM_YX_TRANSPORT { YX_FILE = 0, YX_WEB = 1 };

input ENUM_YX_TRANSPORT Transport  = YX_FILE;   // file = same computer, web = HTTPS to the dashboard
input string  WebURL     = "https://your-dashboard.example.com/api/mt5/bridge"; // web transport only
input string  BridgeToken = "";                  // web transport: token from System > MT5 connection
input string  BasicAuth  = "";                   // web transport behind a password: user:password (optional)
input int     PollMs     = 25;                   // file transport: how often to look for commands (ms)
input int     StateMs    = 500;                  // how often to report account / positions / quotes (ms)

CTrade   trade;
string   g_watch[];          // MT5 symbols YONEXIS wants quotes for
string   g_results = "";     // web: results waiting to be sent (JSON objects, comma separated)
ulong    g_lastState = 0;
bool     g_sendSymbols = false;
string   g_symbolsJson = "";
long     g_seq = 1;          // file transport: number of the next command file (in\0000000001.cmd ...)
string   g_diag = "";        // last problem, reported in state.json
ulong    g_lastHint = 0;
bool     g_symBuilding = false;   // symbol list being built, a slice per timer tick
int      g_symIdx = 0, g_symTotal = 0;
string   g_symBuf = "";

//+------------------------------------------------------------------+
string Esc(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   return s;
}
string D(double v) { return DoubleToString(v, 8); }

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetAsyncMode(false);
   trade.LogLevel(LOG_LEVEL_ERRORS);
   if(Transport == YX_FILE)
   {
      FolderCreate("YONEXIS", FILE_COMMON);
      FolderCreate("YONEXIS\\in", FILE_COMMON);
      FolderCreate("YONEXIS\\out", FILE_COMMON);
      g_seq = LoadSeq();
   }
   EventSetMillisecondTimer(Transport == YX_FILE ? (int)MathMax(10, PollMs) : 10);
   Comment("YONEXIS bridge running (", (Transport == YX_FILE ? "file" : "web"), ")\nCommon folder: ",
           TerminalInfoString(TERMINAL_COMMONDATA_PATH), "\\Files\\YONEXIS");
   WriteState();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
}

//+------------------------------------------------------------------+
string TradeModeName()
{
   long m = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(m == ACCOUNT_TRADE_MODE_DEMO)    return "demo";
   if(m == ACCOUNT_TRADE_MODE_CONTEST) return "contest";
   return "real";
}
string MarginModeName()
{
   long m = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(m == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) return "hedging";
   if(m == ACCOUNT_MARGIN_MODE_EXCHANGE)       return "exchange";
   return "netting";
}
string ReasonName(long r)
{
   if(r == DEAL_REASON_SL)     return "sl";
   if(r == DEAL_REASON_TP)     return "tp";
   if(r == DEAL_REASON_SO)     return "stop_out";
   if(r == DEAL_REASON_EXPERT) return "expert";
   if(r == DEAL_REASON_CLIENT || r == DEAL_REASON_MOBILE || r == DEAL_REASON_WEB) return "manual";
   return "other";
}

//+------------------------------------------------------------------+
string StateJson()
{
   string s = "{\"ea\":\"1.14\",\"next_seq\":" + IntegerToString(g_seq) + ",\"diag\":\"" + Esc(g_diag) + "\",\"symbols_progress\":\"" + (g_symBuilding ? IntegerToString(g_symIdx) + "/" + IntegerToString(g_symTotal) : "") + "\",\"time_msc\":" + IntegerToString((long)TimeCurrent() * 1000) +
              ",\"local_msc\":" + IntegerToString((long)GetTickCount64()) +
              ",\"trade_allowed\":" + ((TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED)) ? "true" : "false") +
              ",\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") +
              ",\"common_path\":\"" + Esc(TerminalInfoString(TERMINAL_COMMONDATA_PATH)) + "\"";
   s += ",\"account\":{\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
        ",\"server\":\"" + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\"" +
        ",\"company\":\"" + Esc(AccountInfoString(ACCOUNT_COMPANY)) + "\"" +
        ",\"name\":\"" + Esc(AccountInfoString(ACCOUNT_NAME)) + "\"" +
        ",\"currency\":\"" + Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\"" +
        ",\"trade_mode\":\"" + TradeModeName() + "\",\"margin_mode\":\"" + MarginModeName() + "\"" +
        ",\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) +
        ",\"balance\":" + D(AccountInfoDouble(ACCOUNT_BALANCE)) + ",\"equity\":" + D(AccountInfoDouble(ACCOUNT_EQUITY)) +
        ",\"margin\":" + D(AccountInfoDouble(ACCOUNT_MARGIN)) + ",\"margin_free\":" + D(AccountInfoDouble(ACCOUNT_MARGIN_FREE)) + "}";
   // positions
   s += ",\"positions\":[";
   int n = PositionsTotal();
   bool firstp = true;
   for(int i = 0; i < n; i++)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(!firstp) s += ",";
      firstp = false;
      s += "{\"ticket\":" + IntegerToString((long)t) +
           ",\"symbol\":\"" + Esc(PositionGetString(POSITION_SYMBOL)) + "\"" +
           ",\"type\":\"" + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell") + "\"" +
           ",\"volume\":" + D(PositionGetDouble(POSITION_VOLUME)) + ",\"price_open\":" + D(PositionGetDouble(POSITION_PRICE_OPEN)) +
           ",\"price\":" + D(PositionGetDouble(POSITION_PRICE_CURRENT)) +
           ",\"sl\":" + D(PositionGetDouble(POSITION_SL)) + ",\"tp\":" + D(PositionGetDouble(POSITION_TP)) +
           ",\"profit\":" + D(PositionGetDouble(POSITION_PROFIT)) +
           ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
           ",\"time_msc\":" + IntegerToString(PositionGetInteger(POSITION_TIME_MSC)) +
           ",\"comment\":\"" + Esc(PositionGetString(POSITION_COMMENT)) + "\"}";
   }
   s += "]";
   // today's deals
   s += ",\"deals\":[";
   datetime from = iTime(_Symbol, PERIOD_D1, 0);
   if(from == 0) from = TimeCurrent() - 86400;
   bool first = true;
   if(HistorySelect(from, TimeCurrent() + 86400))
   {
      int dn = HistoryDealsTotal();
      int start = MathMax(0, dn - 300);
      for(int j = start; j < dn; j++)
      {
         ulong d = HistoryDealGetTicket(j);
         if(d == 0) continue;
         long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
         if(!first) s += ",";
         first = false;
         s += "{\"ticket\":" + IntegerToString((long)d) +
              ",\"position\":" + IntegerToString(HistoryDealGetInteger(d, DEAL_POSITION_ID)) +
              ",\"symbol\":\"" + Esc(HistoryDealGetString(d, DEAL_SYMBOL)) + "\"" +
              ",\"entry\":\"" + (entry == DEAL_ENTRY_IN ? "in" : (entry == DEAL_ENTRY_OUT ? "out" : (entry == DEAL_ENTRY_INOUT ? "inout" : "other"))) + "\"" +
              ",\"type\":\"" + (HistoryDealGetInteger(d, DEAL_TYPE) == DEAL_TYPE_BUY ? "buy" : "sell") + "\"" +
              ",\"reason\":\"" + ReasonName(HistoryDealGetInteger(d, DEAL_REASON)) + "\"" +
              ",\"price\":" + D(HistoryDealGetDouble(d, DEAL_PRICE)) + ",\"volume\":" + D(HistoryDealGetDouble(d, DEAL_VOLUME)) +
              ",\"profit\":" + D(HistoryDealGetDouble(d, DEAL_PROFIT)) + ",\"commission\":" + D(HistoryDealGetDouble(d, DEAL_COMMISSION)) +
              ",\"swap\":" + D(HistoryDealGetDouble(d, DEAL_SWAP)) +
              ",\"magic\":" + IntegerToString(HistoryDealGetInteger(d, DEAL_MAGIC)) +
              ",\"time_msc\":" + IntegerToString(HistoryDealGetInteger(d, DEAL_TIME_MSC)) +
              ",\"comment\":\"" + Esc(HistoryDealGetString(d, DEAL_COMMENT)) + "\"}";
      }
   }
   s += "]";
   // quotes of the watched symbols
   s += ",\"quotes\":{";
   int w = ArraySize(g_watch);
   bool firstq = true;
   for(int k = 0; k < w; k++)
   {
      MqlTick tk;
      if(!SymbolInfoTick(g_watch[k], tk)) continue;
      if(!firstq) s += ",";
      firstq = false;
      s += "\"" + Esc(g_watch[k]) + "\":{\"bid\":" + D(tk.bid) + ",\"ask\":" + D(tk.ask) + ",\"last\":" + D(tk.last) +
           ",\"time_msc\":" + IntegerToString(tk.time_msc) +
           ",\"digits\":" + IntegerToString(SymbolInfoInteger(g_watch[k], SYMBOL_DIGITS)) +
           ",\"volume_min\":" + D(SymbolInfoDouble(g_watch[k], SYMBOL_VOLUME_MIN)) +
           ",\"volume_step\":" + D(SymbolInfoDouble(g_watch[k], SYMBOL_VOLUME_STEP)) +
           ",\"contract\":" + D(SymbolInfoDouble(g_watch[k], SYMBOL_TRADE_CONTRACT_SIZE)) + "}";
   }
   s += "}}";
   return s;
}

// The symbol list is built a slice at a time (a demo server has thousands of symbols): the EA keeps executing
// orders and reporting state while it builds, instead of freezing for many seconds.

void SymbolsStart()
{
   g_symBuilding = true;
   g_symIdx = 0;
   g_symTotal = SymbolsTotal(false);
   g_symBuf = "";
}

void SymbolsStep()
{
   if(!g_symBuilding) return;
   int end = MathMin(g_symTotal, g_symIdx + 250);
   for(int i = g_symIdx; i < end; i++)
   {
      string nm = SymbolName(i, false);
      if(i > 0) StringAdd(g_symBuf, ",");
      StringAdd(g_symBuf, "{\"name\":\"" + Esc(nm) + "\",\"path\":\"" + Esc(SymbolInfoString(nm, SYMBOL_PATH)) + "\"" +
                ",\"desc\":\"" + Esc(SymbolInfoString(nm, SYMBOL_DESCRIPTION)) + "\"" +
                ",\"digits\":" + IntegerToString(SymbolInfoInteger(nm, SYMBOL_DIGITS)) +
                ",\"volume_min\":" + D(SymbolInfoDouble(nm, SYMBOL_VOLUME_MIN)) +
                ",\"volume_step\":" + D(SymbolInfoDouble(nm, SYMBOL_VOLUME_STEP)) +
                ",\"contract\":" + D(SymbolInfoDouble(nm, SYMBOL_TRADE_CONTRACT_SIZE)) + "}");
   }
   g_symIdx = end;
   if(g_symIdx >= g_symTotal)
   {
      g_symBuilding = false;
      string js = "{\"symbols\":[" + g_symBuf + "]}";
      g_symBuf = "";
      if(Transport == YX_FILE) WriteCommonFile("YONEXIS\\symbols.json", js);
      else { g_symbolsJson = js; g_sendSymbols = true; }
   }
}

//+------------------------------------------------------------------+
// open-to-test instead of FileIsExist: under Wine on a Mac FileIsExist does not see files written by other programs
bool TryRead(string name, string &text)
{
   ResetLastError();
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return false;
   text = "";
   while(!FileIsEnding(h)) text += FileReadString(h);
   FileClose(h);
   return true;
}
string ReadCommonFile(string name)
{
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) { g_diag = "cannot open " + name + " (error " + IntegerToString(GetLastError()) + ")"; return ""; }
   string t = "";
   while(!FileIsEnding(h)) t += FileReadString(h);
   FileClose(h);
   return t;
}
long LoadSeq()
{
   string t = "";
   if(!TryRead("YONEXIS\\ea_seq.txt", t)) return 1;
   long v = StringToInteger(t);
   return v > 0 ? v : 1;
}
string SeqName(long n) { return "YONEXIS\\c" + IntegerToString(n, 10, '0') + ".txt"; }

//+------------------------------------------------------------------+
bool WriteCommonFile(string name, string text)
{
   string tmp = name + ".tmp";
   int h = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) return false;
   FileWriteString(h, text);
   FileClose(h);
   return FileMove(tmp, FILE_COMMON, name, FILE_COMMON | FILE_REWRITE);
}

void WriteState()
{
   if(Transport == YX_FILE)
      WriteCommonFile("YONEXIS\\state.json", StateJson());
   g_lastState = GetTickCount64();
}

//+------------------------------------------------------------------+
double Num(string v) { return (StringLen(v) == 0) ? 0.0 : StringToDouble(v); }

double NormVolume(string sym, double v)
{
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double mn = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(step > 0) v = MathRound(v / step) * step;
   if(v < mn) v = mn;
   if(mx > 0 && v > mx) v = mx;
   return NormalizeDouble(v, 8);
}

double NormPrice(string sym, double p)
{
   if(p <= 0) return 0.0;
   double tick = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   if(tick > 0) p = MathRound(p / tick) * tick;
   return NormalizeDouble(p, digits);
}

// result JSON for one command
string Result(string id, bool ok, string extra)
{
   uint rc = trade.ResultRetcode();
   return "{\"id\":\"" + id + "\",\"ok\":" + (ok ? "true" : "false") + ",\"retcode\":" + IntegerToString((long)rc) +
          ",\"desc\":\"" + Esc(trade.ResultRetcodeDescription()) + "\"" + extra + "}";
}

// id|op|symbol|volume|sl|tp|ticket|magic|deviation|comment
string Execute(string line)
{
   string f[];
   StringTrimRight(line);
   StringTrimLeft(line);
   int n = StringSplit(line, '|', f);
   if(n < 2) return "";
   while(ArraySize(f) < 11) { int k = ArraySize(f); ArrayResize(f, k + 1); f[k] = ""; }
   string id = f[0], op = f[1], sym = f[2], comment = f[9];
   double vol = Num(f[3]), sl = Num(f[4]), tp = Num(f[5]);
   ulong ticket = (ulong)StringToInteger(f[6]);
   long magic = StringToInteger(f[7]);
   int dev = (int)StringToInteger(f[8]);
   ulong t0 = GetTickCount64();
   long expires = StringToInteger(f[10]);
   if(expires > 0 && (long)TimeGMT() > expires && op != "ping")
      return "{\"id\":\"" + id + "\",\"ok\":false,\"retcode\":0,\"desc\":\"command expired before MT5 picked it up - not executed\"}";
   trade.SetExpertMagicNumber(magic);
   if(dev > 0) trade.SetDeviationInPoints(dev);

   if(op == "ping")
      return "{\"id\":\"" + id + "\",\"ok\":true,\"pong\":" + IntegerToString((long)GetTickCount64()) + "}";
   if(op == "symbols")
   {
      SymbolsStart();
      return "{\"id\":\"" + id + "\",\"ok\":true,\"started\":true,\"count\":" + IntegerToString(g_symTotal) + "}";
   }
   if(op == "watch")
   {
      string w[];
      int m = StringSplit(sym, ',', w);
      ArrayResize(g_watch, 0);
      string missing = "";
      for(int i = 0; i < m; i++)
      {
         if(StringLen(w[i]) == 0) continue;
         if(!SymbolSelect(w[i], true)) { missing += (StringLen(missing) ? "," : "") + w[i]; continue; }
         int k = ArraySize(g_watch);
         ArrayResize(g_watch, k + 1);
         g_watch[k] = w[i];
      }
      return "{\"id\":\"" + id + "\",\"ok\":true,\"watching\":" + IntegerToString(ArraySize(g_watch)) +
             ",\"missing\":\"" + Esc(missing) + "\"}";
   }
   if(op == "buy" || op == "sell")
   {
      if(!SymbolSelect(sym, true))
         return "{\"id\":\"" + id + "\",\"ok\":false,\"retcode\":0,\"desc\":\"symbol " + Esc(sym) + " not found in MT5\"}";
      trade.SetTypeFillingBySymbol(sym);
      double v = NormVolume(sym, vol);
      bool ok = (op == "buy") ? trade.Buy(v, sym, 0.0, NormPrice(sym, sl), NormPrice(sym, tp), comment)
                              : trade.Sell(v, sym, 0.0, NormPrice(sym, sl), NormPrice(sym, tp), comment);
      uint rc = trade.ResultRetcode();
      ok = ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED || rc == TRADE_RETCODE_DONE_PARTIAL);
      ulong deal = trade.ResultDeal();
      long pos = 0;
      if(deal > 0 && HistoryDealSelect(deal)) pos = HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      if(pos == 0) pos = (long)trade.ResultOrder();
      return Result(id, ok, ",\"order\":" + IntegerToString((long)trade.ResultOrder()) + ",\"deal\":" + IntegerToString((long)deal) +
                    ",\"position\":" + IntegerToString(pos) + ",\"price\":" + D(trade.ResultPrice()) + ",\"volume\":" + D(trade.ResultVolume()) +
                    ",\"exec_ms\":" + IntegerToString((long)(GetTickCount64() - t0)));
   }
   if(op == "modify")
   {
      bool ok = PositionSelectByTicket(ticket) && trade.PositionModify(ticket, NormPrice(sym, sl), NormPrice(sym, tp));
      return Result(id, ok, ",\"exec_ms\":" + IntegerToString((long)(GetTickCount64() - t0)));
   }
   if(op == "close" || op == "close_partial")
   {
      bool ok = false;
      if(ticket > 0)
      {
         if(PositionSelectByTicket(ticket))
            ok = (vol > 0 && vol < PositionGetDouble(POSITION_VOLUME)) ? trade.PositionClosePartial(ticket, NormVolume(PositionGetString(POSITION_SYMBOL), vol))
                                                                      : trade.PositionClose(ticket);
         else
            return "{\"id\":\"" + id + "\",\"ok\":true,\"desc\":\"position already closed\"}";
      }
      else
      {  // netting: by symbol (+ magic), optional partial volume
         bool found = false;
         for(int i = PositionsTotal() - 1; i >= 0; i--)
         {
            ulong t = PositionGetTicket(i);
            if(t == 0 || PositionGetString(POSITION_SYMBOL) != sym || PositionGetInteger(POSITION_MAGIC) != magic) continue;
            found = true;
            double pv = PositionGetDouble(POSITION_VOLUME);
            ok = (vol > 0 && vol < pv) ? trade.PositionClosePartial(t, NormVolume(sym, vol)) : trade.PositionClose(t);
            break;
         }
         if(!found) return "{\"id\":\"" + id + "\",\"ok\":true,\"desc\":\"no open position\"}";
      }
      return Result(id, ok, ",\"price\":" + D(trade.ResultPrice()) + ",\"exec_ms\":" + IntegerToString((long)(GetTickCount64() - t0)));
   }
   if(op == "close_all")
   {
      int closed = 0, failed = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong t = PositionGetTicket(i);
         if(t == 0 || PositionGetInteger(POSITION_MAGIC) != magic) continue;
         if(trade.PositionClose(t)) closed++; else failed++;
      }
      return "{\"id\":\"" + id + "\",\"ok\":" + (failed == 0 ? "true" : "false") + ",\"closed\":" + IntegerToString(closed) +
             ",\"failed\":" + IntegerToString(failed) + ",\"exec_ms\":" + IntegerToString((long)(GetTickCount64() - t0)) + "}";
   }
   return "{\"id\":\"" + id + "\",\"ok\":false,\"desc\":\"unknown command " + Esc(op) + "\"}";
}

//+------------------------------------------------------------------+
void FileLoop()
{
   bool any = false;
   // next.txt holds the number of YONEXIS's latest command: only try to open command files when there is one
   // (a failed FileOpen is slow under Wine, so we never poll for files that do not exist)
   string hint0 = "";
   long latest = TryRead("YONEXIS\\next.txt", hint0) ? StringToInteger(hint0) : 0;
   for(int k = 0; k < 50 && g_seq <= latest; k++)
   {
      string path = SeqName(g_seq);
      string line = "";
      if(!TryRead(path, line)) break;                    // no such command yet
      StringTrimRight(line); StringTrimLeft(line);
      if(StringLen(line) == 0) break;                    // still being written: try again next tick
      FileDelete(path, FILE_COMMON);
      g_seq++;
      WriteCommonFile("YONEXIS\\ea_seq.txt", IntegerToString(g_seq));
      string res = Execute(line);
      if(StringLen(res) > 0)
      {
         string parts[];
         StringSplit(line, '|', parts);
         string id = ArraySize(parts) > 0 ? parts[0] : IntegerToString(g_seq - 1);
         WriteCommonFile("YONEXIS\\r_" + id + ".json", res);
      }
      g_diag = "";
      any = true;
   }
   // re-sync once a second: YONEXIS writes the number of its latest command to next.txt
   if(!any && GetTickCount64() - g_lastHint > 1000)
   {
      g_lastHint = GetTickCount64();
      string hint = "", tmp = "";
      if(TryRead("YONEXIS\\next.txt", hint))
      {
         long last = StringToInteger(hint);
         if(last >= g_seq && last - g_seq <= 500)
         {
            bool found = false;
            for(long n = g_seq + 1; n <= last; n++)
               if(TryRead(SeqName(n), tmp)) { g_diag = "skipped to command " + IntegerToString(n); g_seq = n; found = true; break; }
            if(!found && last > g_seq) g_diag = "waiting for command " + IntegerToString(g_seq) + " (YONEXIS is at " + IntegerToString(last) + ")";
         }
         else if(last - g_seq > 500)
         {
            g_seq = last;                                  // far behind (e.g. EA reinstalled): jump to YONEXIS's latest
            g_diag = "jumped to command " + IntegerToString(last);
         }
      }
   }
   if(any || GetTickCount64() - g_lastState >= (ulong)StateMs) WriteState();
}

void WebLoop()
{
   bool stateDue = GetTickCount64() - g_lastState >= (ulong)StateMs;
   string body = "{\"results\":[" + g_results + "]";
   if(stateDue || StringLen(g_results) > 0) body += ",\"state\":" + StateJson();
   if(g_sendSymbols) { body += ",\"symbols\":" + StringSubstr(g_symbolsJson, 11, StringLen(g_symbolsJson) - 12); g_sendSymbols = false; }
   body += "}";
   g_results = "";
   char data[], result[];
   string rh;
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(data, ArraySize(data) - 1);                 // drop the terminating zero
   string headers = "Content-Type: application/json\r\nX-Bridge-Token: " + BridgeToken + "\r\n";
   if(StringLen(BasicAuth) > 0)
   {
      uchar src[], dst[], key[];
      StringToCharArray(BasicAuth, src, 0, StringLen(BasicAuth), CP_UTF8);
      CryptEncode(CRYPT_BASE64, src, key, dst);
      headers += "Authorization: Basic " + CharArrayToString(dst) + "\r\n";
   }
   int code = WebRequest("POST", WebURL, headers, 5000, data, result, rh);
   g_lastState = GetTickCount64();
   if(code != 200)
   {
      if(code == -1) Comment("YONEXIS bridge: WebRequest failed (", GetLastError(), ") - add ", WebURL, " in Tools > Options > Expert Advisors");
      else Comment("YONEXIS bridge: dashboard answered HTTP ", code);
      return;
   }
   string text = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   string lines[];
   int n = StringSplit(text, '\n', lines);
   for(int i = 0; i < n; i++)
   {
      if(StringLen(lines[i]) < 3) continue;
      string res = Execute(lines[i]);
      if(StringLen(res) > 0) g_results += (StringLen(g_results) ? "," : "") + res;
   }
}

void OnTimer()
{
   SymbolsStep();
   if(Transport == YX_FILE) FileLoop();
   else WebLoop();
}

// report position changes immediately (SL / TP fills) instead of waiting for the next state tick
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD) g_lastState = 0;
}
//+------------------------------------------------------------------+
