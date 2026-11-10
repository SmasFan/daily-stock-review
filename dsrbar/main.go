// DSR Bar —— 常驻 macOS 菜单栏的「每日复盘 / 币安量化」看板。
//
// 菜单栏文字实时显示策略账户的累计收益与持仓数，点图标弹出看板窗口
// （WebView 加载本机 8000 端口的页面），右键是明细菜单。
//
// 数据源：binance-llm-bot/status_page.py 每 30s 写出的 data/binance_status.json。
package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/egoist/mygo"
)

//go:embed resources/tray.png
var trayPNG []byte

func dbg(format string, a ...any) {
	f, err := os.OpenFile("/tmp/dsrbar.log", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s %s\n", time.Now().Format("15:04:05"), fmt.Sprintf(format, a...))
}

const (
	baseURL    = "http://127.0.0.1:8000"
	cmdAddr    = "127.0.0.1:8765"
	statusPath = "/Users/smas/code/daily-stock-review/data/binance_status.json"
	refreshSec = 15
)

type Position struct {
	Symbol   string  `json:"symbol"`
	Name     string  `json:"name"`
	Side     string  `json:"side"`
	Amt      float64 `json:"amt"`
	Entry    float64 `json:"entry"`
	Mark     float64 `json:"mark"`
	PnlPct   float64 `json:"pnlPct"`
	PnlUsd   float64 `json:"pnlUsd"`
	Lev      float64 `json:"lev"`
	Notional float64 `json:"notional"`
	Pool     string  `json:"pool"`
}

type Bucket struct {
	Start      float64 `json:"start"`
	Pool       float64 `json:"pool"`
	Pnl        float64 `json:"pnl"`
	PnlPct     float64 `json:"pnlPct"`
	Realized   float64 `json:"realized"`
	Unrealized float64 `json:"unrealized"`
	N          int     `json:"n"`
	Slots      int     `json:"slots"`
}

type Status struct {
	GeneratedAt string `json:"generatedAt"`
	Account     struct {
		Start      float64 `json:"start"`
		Equity     float64 `json:"equity"`
		Pnl        float64 `json:"pnl"`
		PnlPct     float64 `json:"pnlPct"`
		TodayRet   float64 `json:"todayRet"`
		DayOpen    float64 `json:"dayOpen"`
		DayDate    string  `json:"dayDate"`
		DemoWallet float64 `json:"demoWallet"`
	} `json:"account"`
	Daily      Bucket     `json:"daily"`
	Short      Bucket     `json:"short"`
	Positions  []Position `json:"positions"`
	NPos       int        `json:"nPos"`
	NLong      int        `json:"nLong"`
	NShort     int        `json:"nShort"`
	TodayTrade int        `json:"todayTrades"`

	ok      bool
	loadErr string
}

var (
	mu       sync.Mutex // 保护 current
	flyMu    sync.Mutex // 保护 flyout 指针（AppKit 回调可能不在主线程）
	current  = &Status{}
	tray     *mygo.Tray
	win      *mygo.Window
	flyout   *mygo.Window
	quitting bool
)

func loadStatus() *Status {
	s := &Status{}
	b, err := os.ReadFile(statusPath)
	if err != nil {
		s.loadErr = err.Error()
		return s
	}
	if err := json.Unmarshal(b, s); err != nil {
		s.loadErr = err.Error()
		return s
	}
	s.ok = true
	return s
}

// titleText 菜单栏文字。收益固定 5 字符宽（%+5.1f），避免数字跳动时图标左右位移。
func titleText(s *Status) string {
	if !s.ok {
		return "— 离线"
	}
	return fmt.Sprintf("%+5.1f%% · %d仓", s.Account.PnlPct, s.NPos)
}

func tooltip(s *Status) string {
	if !s.ok {
		return "DSR Bar：读不到 binance_status.json\n" + s.loadErr
	}
	return fmt.Sprintf("DSR 策略账户 %.2f U（%+.2f%%）\n今日 %+.2f%% · 持仓 %d（多 %d / 空 %d）\n更新 %s",
		s.Account.Equity, s.Account.PnlPct, s.Account.TodayRet, s.NPos, s.NLong, s.NShort, s.GeneratedAt)
}

func refresh() {
	s := loadStatus()
	mu.Lock()
	current = s
	mu.Unlock()
	if tray != nil {
		tray.SetTitle(titleText(s))
		tray.SetToolTip(tooltip(s))
	}
}

func openPage(path, title string) {
	if win == nil {
		return
	}
	win.SetTitle(title)
	if p := win.Page(); p != nil {
		_ = p.LoadURL(baseURL + path)
	}
	win.Show()
	mygo.App.Focus()
}

func toggleWindow() {
	if win == nil {
		return
	}
	if win.IsVisible() {
		win.Hide()
		return
	}
	win.Show()
	mygo.App.Focus()
}

// startCmdServer 给小卡片一个「点按钮 → 打开完整看板」的通道。
// WKWebView 里没有 JS bridge，用只监听 127.0.0.1 的极简 HTTP 端点最稳；
// 页面 fetch 是简单 GET，返回 CORS 头即可，动作严格白名单。
func startCmdServer() {
	mux := http.NewServeMux()
	mux.HandleFunc("/cmd", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Cache-Control", "no-store")
		action := r.URL.Query().Get("action")
		switch action {
		case "dashboard":
			openPage("/binance.html", "币安 Bot · 量化看板")
		case "sim":
			openPage("/sim_live.html", "实时模拟盘")
		case "dividend":
			openPage("/sim_dividend.html", "红利低波池 · 实时模拟")
		case "perp":
			openPage("/perp.html", "全天候合约盘")
		case "index":
			openPage("/index.html", "每日复盘工作台")
		case "close":
			flyMu.Lock()
			f := flyout
			flyMu.Unlock()
			if f != nil && !f.IsDestroyed() {
				f.Hide()
			}
		case "hidewin":
			// 自检/运维：把完整看板窗口收起来（不退出 app，菜单栏仍在）
			if win != nil {
				win.Hide()
			}
		case "refresh":
			refresh()
		case "ping":
			// 自检：页面里的按钮能不能真的调到这个端点（WKWebView / ATS）
		case "click":
			// 自检：卡片内策略行的点击有没有真的进到 WKWebView
			dbg("cmd click %s", r.URL.Query().Get("k"))
		default:
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		dbg("cmd %s", action)
		w.WriteHeader(http.StatusNoContent)
	})
	go func() {
		if err := http.ListenAndServe(cmdAddr, mux); err != nil {
			dbg("cmd server: %v", err)
		}
	}()
	dbg("cmd server on %s", cmdAddr)
}

func posLine(p Position) string {
	dir := "多"
	if p.Side == "short" {
		dir = "空"
	}
	return fmt.Sprintf("%s %s %.4g @%.4g  %+.2f%%  (%+.2f U)", p.Name, dir, p.Amt, p.Entry, p.PnlPct, p.PnlUsd)
}

func buildMenu() *mygo.Menu {
	mu.Lock()
	s := current
	mu.Unlock()

	acctLabel := "等待币安状态页写入数据…"
	dailyLabel, shortLabel := "日线池 —", "短线池 —"
	var posItems []*mygo.MenuItem
	if s.ok {
		acctLabel = fmt.Sprintf("账户 %.2f U  %+.2f%%（今日 %+.2f%%）", s.Account.Equity, s.Account.PnlPct, s.Account.TodayRet)
		dailyLabel = fmt.Sprintf("日线池 %.2f U  %+.2f%%  %d/%d", s.Daily.Pool, s.Daily.PnlPct, s.Daily.N, s.Daily.Slots)
		shortLabel = fmt.Sprintf("短线池 %.2f U  %+.2f%%  %d/%d", s.Short.Pool, s.Short.PnlPct, s.Short.N, s.Short.Slots)
		for _, p := range s.Positions {
			label := posLine(p)
			posItems = append(posItems, &mygo.MenuItem{Label: label, Disabled: true})
		}
		if len(posItems) == 0 {
			posItems = append(posItems, &mygo.MenuItem{Label: "当前空仓", Disabled: true})
		}
	}
	posMenu := &mygo.MenuItem{Label: fmt.Sprintf("持仓明细（%d）", len(s.Positions)), Submenu: posItems}

	items := []*mygo.MenuItem{
		{Label: acctLabel, Disabled: true},
		{Label: dailyLabel, Disabled: true},
		{Label: shortLabel, Disabled: true},
		mygo.Separator(),
		posMenu,
		mygo.Separator(),
		{Label: "打开看板", Accelerator: "CmdOrCtrl+O", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/binance.html", "币安 Bot · 量化看板")
		}},
		{Label: "币安 Bot", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/binance.html", "币安 Bot · 量化看板")
		}},
		{Label: "红利池", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/sim_dividend.html", "红利低波池 · 实时模拟")
		}},
		{Label: "实时模拟（双池）", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/sim_live.html", "实时模拟盘")
		}},
		{Label: "全天候合约盘", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/perp.html", "全天候合约盘")
		}},
		{Label: "工作台", Click: func(*mygo.MenuItem, *mygo.Window) {
			openPage("/index.html", "每日复盘工作台")
		}},
		mygo.Separator(),
		{Label: "立即刷新", Accelerator: "CmdOrCtrl+R", Click: func(*mygo.MenuItem, *mygo.Window) {
			refresh()
		}},
		mygo.Separator(),
		{Role: mygo.RoleQuit},
	}
	return mygo.NewMenu(items)
}

func createTray() {
	t, err := mygo.NewTray(mygo.TrayOptions{
		ID:             "dsr-main",
		Icon:           trayPNG,
		IconIsTemplate: true,
		Title:          titleText(current),
		ToolTip:        tooltip(current),
	})
	if err != nil {
		dbg("tray error: %v", err)
		log.Println("tray:", err)
		return
	}
	tray = t
	dbg("tray created, title=%q", titleText(current))
	t.OnClick(toggleFlyout)
	t.OnRightClick(func() {
		t.SetMenu(buildMenu())
		t.PopUpMenu()
		t.SetMenu(nil)
	})
}

// cardQuery 只给调试/截图用：DSRBAR_TAB=bn|dv|sl 决定小卡片默认页签。
func cardQuery() string {
	if q := os.Getenv("DSRBAR_QUERY"); q != "" {
		return "?" + strings.TrimPrefix(q, "?")
	}
	if t := os.Getenv("DSRBAR_TAB"); t == "dv" || t == "sl" {
		return "?tab=" + t
	}
	return ""
}

// createFlyout 建的是「菜单栏状态项面板」：锚在图标下方、系统 Popover 材质与箭头、
// 点面板外自动收起，跟 Magpie 那种小卡片一样。
func createFlyout() *mygo.Window {
	w := mygo.NewFlyout(mygo.FlyoutOptions{
		Tray:      tray,
		Placement: mygo.PlacementBottom,
		Width:     380,
		Height:    560, // 全池持仓/买点比 6 股池多，留出首屏同时看到两块的余量
		Focusable: true,
		Popover:   true,
		Shadow:    true,
		URL:       baseURL + "/dsrbar_card.html" + cardQuery(),
		Hidden:    true,
	})
	flyMu.Lock()
	flyout = w
	flyMu.Unlock()
	// 点面板外 / 按 Esc / 打开大窗口抢焦点时，AppKit 会自己关掉 transient
	// NSPopover。MyGo 在 popoverDidClose: 里调 cleanup() + h.Closed()，把
	// WKWebView 和 native 窗口全部销毁（window.go windowHandler.Closed →
	// w.native=nil, destroyed=true，并从 windows.list 移除）。此后对这个
	// Window 调 Show() 是无效的 —— 这正是「点出来一次就再也点不出来」的
	// 真因。唯一安全的关闭方式是我们主动 Hide()（hidePopover 置
	// popoverClosing=true，delegate 直接 return，不销毁）。
	// 主防线：AppKit 因点面板外 / 按 Esc / 点锚点想关闭 transient popover
	// 时会先问 popoverShouldClose: → MyGo 的 h.ShouldClose() → 这里 OnClose。
	// 一律 veto（否则走 popoverDidClose: 的 cleanup 分支销毁窗口），然后
	// 自己异步 Hide()：hidePopover 会置 popoverClosing=true，delegate 直接
	// return，窗口保持可复用，下次 Show() 一定有效。
	w.OnClose(func(e *mygo.CloseEvent) {
		if quitting {
			return
		}
		e.PreventDefault()
		dbg("flyout close vetoed -> safe Hide")
		// 必须捕获本次事件对应的窗口实例 w，绝不能读全局 flyout：
		// 若 30ms 内窗口被销毁并重建，读全局会把刚 Show 的新窗口误关掉。
		// IsVisible() 检查让连续多次外部点击只落一次 Hide。
		go func() {
			time.Sleep(30 * time.Millisecond)
			if !w.IsDestroyed() && w.IsVisible() {
				w.Hide()
			}
		}()
	})
	// 副防线：万一别的路径（打开大窗口抢焦点等）还是销毁了窗口，
	// 清空引用，下次点击重建一个新窗口；只在仍是当前实例时清空，
	// 防止旧窗口的回调误清新实例。
	w.OnClosed(func() {
		if quitting {
			return
		}
		dbg("flyout destroyed by AppKit; will recreate on next click")
		flyMu.Lock()
		if flyout == w {
			flyout = nil
		}
		flyMu.Unlock()
	})
	dbg("flyout created")
	return w
}

// toggleFlyout 只按「窗口是否还活着 / 是否可见」判断。
// 不再用任何基于时间的防抖（AppKit 会吞掉锚点上的一次按下，时间窗会把
// 随后一次正常点击也吞掉），也不再复用一个已经被 AppKit 销毁的 Window。
func toggleFlyout() {
	flyMu.Lock()
	f := flyout
	flyMu.Unlock()
	if f == nil || f.IsDestroyed() {
		dbg("fly click: recreate + show")
		createFlyout().Show()
		return
	}
	if f.IsVisible() {
		dbg("fly click: hide")
		f.Hide()
		return
	}
	dbg("fly click: show")
	f.Show()
}

func createWindow() {
	win = mygo.NewWindow(mygo.WindowOptions{
		Title:     "DSR 看板",
		URL:       baseURL + "/binance.html",
		Width:     1180,
		Height:    820,
		MinWidth:  880,
		MinHeight: 560,
		StateKey:  "dsr-main",
	})
	win.OnClose(func(e *mygo.CloseEvent) {
		if quitting {
			return
		}
		e.PreventDefault()
		win.Hide()
	})
	if os.Getenv("DSRBAR_SHOW") == "1" || len(os.Args) > 1 && os.Args[1] == "-show" {
		win.Show()
		mygo.App.Focus()
	} else {
		win.Hide()
	}
}

func main() {
	mygo.App.SetActivationPolicy(mygo.ActivationPolicyAccessory)
	mygo.App.OnBeforeQuit(func(*mygo.QuitEvent) { quitting = true })

	dbg("main: calling Run")
	mygo.App.WhenReady(func() {
		dbg("WhenReady fired")
		refresh()
		dbg("refresh done, ok=%v err=%q", current.ok, current.loadErr)
		createTray()
		createFlyout()
		createWindow()
		startCmdServer()
		if os.Getenv("DSRBAR_PANEL") == "1" {
			go func() {
				time.Sleep(2500 * time.Millisecond)
				flyMu.Lock()
				f := flyout
				flyMu.Unlock()
				if f != nil && !f.IsDestroyed() {
					f.Show()
				}
			}()
		}
		if os.Getenv("DSRBAR_MENU") == "1" {
			go func() {
				time.Sleep(2 * time.Second)
				tray.SetMenu(buildMenu())
				tray.PopUpMenu()
			}()
		}
		go func() {
			t := time.NewTicker(refreshSec * time.Second)
			defer t.Stop()
			for range t.C {
				refresh()
			}
		}()
	})
	mygo.App.Run()
}
