(() => {
  const zh = {
    "Dashboard": "仪表盘", "Trades": "交易", "History": "历史", "Stats": "统计", "Settings": "设置", "Strategy": "策略",
    "Auto trading: ON": "自动交易：开", "Auto trading: OFF": "自动交易：关",
    "Pausing new entries does not stop risk management for open positions.": "暂停新入场后，已有持仓的风险管理仍会继续。",
    "Could not update auto trading": "无法更新自动交易状态",
    "General Settings": "常规设置", "Manage the bot runtime, simulated account and Polymarket account connection.": "管理机器人运行模式、模拟账户和 Polymarket 账户连接。",
    "Bot runtime": "机器人运行设置", "Choose whether the next bot launch simulates trades or submits real orders.": "选择机器人下次启动时模拟交易还是提交真实订单。",
    "Trading mode": "交易模式", "Mode changes take effect after restarting the bot.": "交易模式在重启机器人后生效。",
    "Simulated account": "模拟账户", "Set the starting USDC balance used by the dashboard and Quarter-Kelly sizing.": "设置仪表盘和四分之一凯利仓位计算使用的 USDC 起始余额。",
    "Starting balance · USDC": "起始余额 · USDC", "Trade history stays intact; the displayed simulated cash is recalculated from this starting balance.": "交易历史保留；显示的模拟现金会根据此起始余额重新计算。",
    "Polymarket account": "Polymarket 账户", "Connect the wallet used for Live trading. The private key is saved only to this machine's .env file and is never shown again.": "连接实盘交易使用的钱包。私钥仅保存到本机 .env 文件，保存后不会再次显示。",
    "Wallet private key": "钱包私钥", "Leave blank to keep the saved key": "留空以保留已保存的私钥",
    "A key is saved": "已保存私钥", " and will connect after restart": "，重启后连接", " for the current process": "，当前进程已连接",
    "No key is saved. Enter a 32-byte hexadecimal private key to connect an account.": "尚未保存私钥。输入 32 字节十六进制私钥以连接账户。",
    "Live USDC balance": "实盘 USDC 余额", "View the connected wallet balance in the dashboard header. Account changes require a restart.": "连接的钱包余额显示在仪表盘顶部。账户变更需要重启。",
    "Current process": "当前进程", "Saved startup mode: ": "已保存的启动模式：", "Account key": "账户私钥",
    "Configured": "已配置", "Not configured": "未配置", "Balance changes apply now. Restart with btcbot serve without mode flags to apply saved account and trading mode.": "余额设置立即生效。重启时不带模式参数运行 btcbot serve，以应用已保存的账户和交易模式。",
    "I understand Live mode can place real orders.": "我确认实盘模式可能提交真实订单。", "This confirmation is required before saving Live mode.": "保存实盘模式前需要确认。",
    "Choose Paper or Live trading": "请选择模拟或实盘交易", "Paper starting balance must be a number": "模拟账户起始余额必须为数字",
    "Paper starting balance must be greater than 0": "模拟账户起始余额必须大于 0", "Private key must be text": "私钥必须为文本",
    "Private key must be a 32-byte hexadecimal key": "私钥必须是 32 字节十六进制格式", "Live-trading confirmation must be checked": "请勾选实盘交易确认",
    "Add a Polymarket private key before enabling Live mode": "启用实盘前请先添加 Polymarket 私钥",
    "Confirm that Live mode can place real orders": "请确认实盘模式可能提交真实订单", "Saved · account and trading mode apply after restart": "已保存 · 账户和交易模式将在重启后生效",
    "Settings can only be changed from localhost": "只能从本机更改设置",
    " · current": " · 当前运行",
    "Paper USDC": "模拟 USDC", "Live USDC": "实盘 USDC", "Paper": "模拟", "Live": "实盘",
    "Paper trading": "模拟交易", "Live trading": "实盘交易", "LIVE": "运行中", "OFFLINE": "已离线",
    "Unavailable": "不可用", "No key": "未设置密钥", "Set BOT_PRIVATE_KEY": "请设置 BOT_PRIVATE_KEY",
    "IN POSITION": "持仓中", "RISK PAUSED": "风险暂停", "COOLDOWN": "冷却中", "WARM-UP": "预热中",
    "WAITING MARKET": "等待市场", "MONITORING": "监控中",
    "BTC Spot · Binance live": "BTC 现货 · Binance 实时价格", "— flat": "— 持平", "Active Market": "当前市场", "Refresh market": "刷新市场",
    "s remaining": "秒后结束", "Waiting for market…": "等待市场…", "Signal Win Probability": "信号胜率概率",
    "strength=": "信号强度=", " · net edge=": " · 净优势=", " · Warm-up · trading paused": " · 预热中 · 暂停交易",
    " · Cooldown · trading paused": " · 冷却中 · 暂停交易", "Warm-up · trading paused": "预热中 · 暂停交易",
    "Cooldown · trading paused": "冷却中 · 暂停交易", "No active signal": "暂无有效信号", "Open Position": "持仓",
    "No open position": "当前无持仓", "UP · live midpoint": "看涨 · 实时中间价",
    "DOWN · live midpoint": "看跌 · 实时中间价", "Binance Start · fixed": "Binance 起始价 · 固定",
    "Chainlink Live · Polymarket RTDS": "Chainlink 实时价 · Polymarket RTDS", "RTDS update ": "RTDS 更新于 ",
    "On-chain ref $": "链上参考价 $",
    "Binance Delta": "Binance 价格变化", "Total Net P&L": "累计净盈亏", "Daily P&L": "当日盈亏",
    "Trades (24h)": "交易笔数（24 小时）", "Win Rate": "胜率", "Bot Status": "机器人状态",
    "Recent Trades": "近期交易", "View All →": "查看全部 →", "Time": "时间", "Market": "市场",
    "Type": "类型", "Direction": "方向", "Price": "价格", "Shares": "份额", "Amount": "金额", "Fee": "手续费",
    "Win Probability": "信号胜率概率", "Strength": "强度", "Mode": "模式", "No trades yet.": "暂无交易记录。",
    "No trades yet. The bot will trade when it detects mispricing.": "暂无交易记录。机器人检测到价格偏差后将自动交易。",
    "Trade Log": "交易记录", "Total Trades": "交易总笔数", "Entries": "入场", "Hedges": "对冲次数",
    "All-Time Win Rate": "历史胜率", "Avg Net / Market": "单市场平均净盈亏", "All-Time Net P&L": "历史累计净盈亏",
    "Interval Statistics": "区间统计", "Win rate and net P&L use each market's settlement time.": "胜率和净盈亏按市场结算时间统计。",
    "Start time": "开始时间", "End time": "结束时间", "Apply": "统计", "Markets": "市场数", "Wins / Losses / Hedged": "胜出 / 亏损 / 对冲",
    "Trade pagination": "交易记录分页", "Showing": "显示第", "of": "共", "trades": "笔交易", "Previous": "上一页", "Next": "下一页",
    "P&L": "盈亏", "Result": "结果", "WIN": "胜出", "LOSS": "亏损", "HEDGED": "对冲完成", "SOLD": "已卖出",
    "No trades yet.": "暂无交易记录。", "Performance History": "历史表现", "Best Day": "最佳单日",
    "Worst Day": "最差单日", "Equity Curve (Cumulative P&L)": "资金曲线（累计盈亏）", "Daily Breakdown": "每日汇总",
    "Date": "日期", "Wins": "盈利笔数", "Losses": "亏损笔数", "Hedged": "已对冲", "Net P&L": "净盈亏",
    "No history yet.": "暂无历史记录。", "Cumulative P&L": "累计盈亏", "Net P&L: ": "净盈亏：",
    "Hourly P&L (last 24h)": "每小时盈亏（最近 24 小时）", "Cumulative P&L (last 24h)": "累计盈亏（最近 24 小时）",
    "Hourly P&L": "每小时盈亏", " trades": " 笔交易", "Stats": "统计", "Realized P&L by period": "分时段已实现盈亏",
    "Period": "时段", "Day": "日", "Week": "周", "Month": "月", "All-Time": "全部时间", "Net P&L": "净盈亏",
    "Trades": "交易笔数", "Best Market": "最佳市场", "Worst Market": "最差市场",
    "Equity Curve": "资金曲线", "No trades in this period": "此时段暂无交易", "Outcome Distribution": "交易结果分布",
    "Daily P&L": "每日盈亏", "Weekly P&L": "每周盈亏", "Monthly P&L": "每月盈亏", "P&L": "盈亏",
    "Equity: $": "权益：$", " trade(s)": " 笔交易",
    "Strategy settings": "策略设置", "Select a strategy to view and edit its configuration.": "选择策略后查看并编辑对应配置。",
    "Switch between the two strategies. The selection is saved with the button on the right.": "在两种策略间切换。点击右侧按钮保存所选策略。", "Strategy option": "策略选项",
    "Selected strategy": "已选策略", "Changes save with the button on the right. The selected strategy applies at the next market.": "点击右侧按钮保存更改。所选策略将在下一个市场生效。",
    "BTC Momentum + Regime": "BTC 动量 + 市场状态", "Tilted Pair + Dynamic Exit": "倾斜配对 + 动态退出",
    "Position sizing": "仓位大小", "Choose the size for each new entry. Orders above the USDC cap are rejected.": "设置每次新入场的仓位。超过 USDC 上限的订单将被拒绝。",
    "Position mode": "仓位模式", "Fixed size": "固定仓位", "Quarter-Kelly": "四分之一凯利",
    "Fixed size unit": "固定仓位单位", "USDC amount": "USDC 金额", "USDC per entry": "每次入场 USDC 数量",
    "Shares per entry": "每次入场份额", "Quarter-Kelly minimum · USDC": "四分之一凯利最小值 · USDC",
    "Maximum position · USDC": "最大仓位 · USDC", "Kelly bankroll · USDC": "凯利资金 · USDC", "Used by Quarter-Kelly sizing.": "供四分之一凯利仓位计算使用。",
    "Entry filters": "入场过滤条件", "Adjust existing gates without changing the signal formula.": "调整现有入场条件，不改变信号计算公式。",
    "Minimum signal strength": "最低信号强度", "Reject weaker signals.": "忽略强度更低的信号。", "Minimum edge": "最低优势",
    "Existing regime adjustment still applies.": "仍会应用现有市场状态调整。", "Lowest token price": "代币最低价格",
    "$0.00 disables the lower bound.": "$0.00 表示不限制最低价格。", "Highest token price": "代币最高价格",
    "Risk limits": "风险限制", "Stop new entries when daily or consecutive-loss limits are reached.": "达到每日亏损或连续亏损上限后停止新入场。",
    "Daily loss stop · USDC": "每日亏损止损 · USDC", "Consecutive losses": "连续亏损次数", "Uses the existing cooldown behavior.": "使用现有冷却机制。",
    "Hedge protection": "对冲保护", "Hedges keep the existing trigger and timing, with a per-order budget cap.": "对冲沿用现有触发条件和时机，并限制单笔预算。",
    "Enable hedging": "启用对冲", "Use the existing trigger and safety guards.": "使用现有触发条件和安全保护。", "Price drop trigger": "价格下跌触发阈值",
    "Maximum hedge amount": "最大对冲金额", "Order amount cap; trading fees are separate.": "单笔订单金额上限；交易手续费另计。",
    "Market response": "市场参数", "Adjust volatility, timing and limit-price parameters already used by the bot.": "调整机器人现有的波动率、时间和限价参数。",
    "Expected BTC volatility · 5 min": "预期 BTC 波动率 · 5 分钟", "Regime lookback · markets": "市场状态回看 · 市场数",
    "Warm-up · seconds": "预热时间 · 秒", "Entry cutoff · seconds remaining": "停止入场 · 剩余秒数", "Limit price offset": "限价偏移",
    "Added to the reference price.": "加到参考价格上的偏移量。", "New entry size": "新入场仓位", "Safety rails": "安全限制",
    "Save changes": "保存更改", "Discard edits": "放弃更改", "Changes and strategy selection save together. The selected strategy applies at the next market.": "配置和策略选择将一起保存。所选策略将在下一个市场生效。",
    "Settings can be saved from this machine only.": "只能在此设备上保存设置。", "Unsaved changes": "有未保存的更改", "Saving…": "正在保存…",
    "Saved · selected strategy applies at the next market": "已保存 · 所选策略将在下一个市场生效",
    "Saved · strategy applies at the next market": "已保存 · 策略将在下一个市场生效", "Could not save settings": "无法保存设置",
    "Fixed USDC": "固定 USDC", "Fixed shares": "固定份额", " per entry": " / 次入场", " shares per entry": " 份额 / 次入场",
    "Max $": "最大 $", " · daily stop $": " · 每日止损 $",
    "Base shares · Q": "基础份额 · Q", "Each side starts at Q shares. A calibrated leader receives Q × (1 + tilt).": "每侧以 Q 份开始。校准后的领先侧为 Q × (1 + 倾斜比例)。",
    "One-side share tilt": "单侧份额倾斜比例", "Default 10%. The leader receives this percentage more tokens.": "默认 10%。领先方向按此比例增加代币份额。",
    "Tilt changes token count, not dollar allocation.": "倾斜比例调整代币份额，不改变金额分配。",
    "Active strategy": "当前策略", "Two-sided entry": "双向入场", "Orders use token shares. Both legs must pass price, depth and total-cost checks before they are submitted.": "订单按代币份额下单。两侧订单都通过价格、深度和总成本检查后才会提交。",
    "Maximum protected total cost · USDC": "总成本保护上限 · USDC", "Includes the order price ceiling and modeled fees.": "包含订单价格上限和估算手续费。",
    "Maximum token entry price": "代币最高入场价格", "Minimum calibrated probability gap": "最低校准概率差", "Below this gap, or without enough history, both sides stay at Q.": "低于此差值或历史数据不足时，两侧均保持 Q 份。",
    "Last 60 seconds": "最后 60 秒", "Exit rules stay fixed to the safety checks requested for this strategy.": "退出规则固定遵循此策略的安全检查。",
    "Sell the weak side": "卖出较弱一侧", " only after eight aligned samples confirm direction, both books and price feeds are fresh, and net sale value beats calibrated settlement value.": "仅当连续八次采样确认方向、两侧订单簿和价格数据均为最新，且净卖出价值高于校准结算价值后执行。",
    "Hold both sides": "持有双方", " when momentum or odds reverse, the model is unclear, or calibration and liquidity are insufficient.": "当动量或赔率反转、模型不明确，或校准数据与流动性不足时。",
    "Share plan": "份额计划", "UP leads": "看涨方领先", "DOWN leads": "看跌方领先",
    "Strategy selection applies at the next market. Entry limits apply to new pairs.": "所选策略将在下一个市场生效。入场限制适用于新的配对订单。",
    "Base shares": "基础份额", "Maximum total cost": "总成本上限", "Maximum token price": "代币最高价格", "Minimum probability gap": "最低概率差",
    "Fixed Shares": "固定份额数量", "Paired base shares": "配对基础份额", "Paired maximum cost": "配对最高成本",
    "Paired maximum token price": "配对代币最高价格", "Paired tilt threshold": "配对倾斜阈值", "Minimum position": "最低仓位",
    "Maximum position": "最高仓位", "Bankroll": "资金规模", "BTC volatility": "BTC 波动率", "Daily loss limit": "每日亏损上限",
    "Loss limit": "亏损上限", "Minimum token price": "代币最低价格", "Hedge trigger": "对冲触发阈值",
    "Regime lookback": "市场状态回看", "Entry cutoff": "入场截止", "Hedge setting must be enabled or disabled": "对冲设置必须启用或禁用",
    "Expected a JSON object": "需要 JSON 对象", "Position mode must be fixed or dynamic": "仓位模式必须为固定或动态",
    "Fixed size type must be usd or shares": "固定仓位单位必须为美元金额或份额", "Choose an available strategy": "请选择可用策略",
    "Minimum USDC cannot exceed maximum USDC": "最低 USDC 不能超过最高 USDC", "Minimum token price cannot exceed maximum token price": "代币最低价格不能超过最高价格",
    "Fixed USDC cannot exceed maximum position": "固定 USDC 不能超过最大仓位", " must be a number": "必须是数字",
    " must be a whole number": "必须是整数", " must be ": " 必须是 ",
    " ago": "前", "s ago": "秒前", "m ago": "分钟前", "h ago": "小时前", "d ago": "天前",
    "Cumulative P&L": "累计盈亏", "Net P&L": "净盈亏", "Hourly P&L": "每小时盈亏",
    "— Polymarket 5m BTC Up/Down": "— Polymarket 5 分钟 BTC 涨跌", "Language": "语言"
  };

  const english = Object.fromEntries(Object.entries(zh).map(([en, translated]) => [translated, en]));
  const sorted = map => Object.entries(map).sort(([a], [b]) => b.length - a.length);
  const maps = { zh: sorted(zh), en: sorted(english) };
  let language = "en";
  try { language = localStorage.getItem("btcbot-language") === "zh" ? "zh" : "en"; } catch (_) {}

  const translateText = text => {
    for (const [from, to] of maps[language]) text = text.split(from).join(to);
    return text;
  };
  window.translateUI = translateText;
  window.uiLanguage = language;

  function translate(root) {
    if (root.nodeType === Node.TEXT_NODE) {
      if (!/^(SCRIPT|STYLE)$/.test(root.parentElement?.tagName || "")) root.data = translateText(root.data);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    const attrs = ["title", "aria-label", "placeholder", "alt"];
    const elements = [root, ...root.querySelectorAll("[title], [aria-label], [placeholder], [alt]")];
    for (const element of elements) {
      for (const name of attrs) {
        if (element.hasAttribute(name)) element.setAttribute(name, translateText(element.getAttribute(name)));
      }
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!/^(SCRIPT|STYLE)$/.test(node.parentElement?.tagName || "")) node.data = translateText(node.data);
    }
  }

  function setLanguage(next, save = true) {
    language = next === "zh" ? "zh" : "en";
    window.uiLanguage = language;
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    if (save) try { localStorage.setItem("btcbot-language", language); } catch (_) {}
    const select = document.getElementById("language-select");
    if (select) select.value = language;
    translate(document.documentElement);
    document.dispatchEvent(new CustomEvent("ui-languagechange", { detail: { language } }));
  }

  setLanguage(language, false);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(translate)))
    .observe(document.body, { childList: true, subtree: true });
  document.getElementById("language-select")?.addEventListener("change", event => setLanguage(event.target.value));
})();
