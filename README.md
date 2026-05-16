# SCALPING BOT — Binance Testnet Spot
## Arquitectura y Guía de Uso

---

## 📁 Estructura

```
scalping_bot/
├── config.py        → Toda la configuración (API keys, pares, parámetros)
├── indicators.py    → Cálculo RSI, MACD, EMA, Bollinger Bands
├── executor.py      → Gestión de órdenes REST API + control de posiciones
├── logger.py        → CSV de trades + alertas Telegram
├── bot.py           → Núcleo: WebSocket + event loop principal
└── requirements.txt → Dependencias
```

---

## ⚙️ Setup

### 1. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 2. Obtener API Keys del Testnet
1. Ir a https://testnet.binance.vision/
2. Login con GitHub
3. Generar API Key + Secret
4. Pegar en `config.py`:
   ```python
   API_KEY    = "tu_key_aqui"
   API_SECRET = "tu_secret_aqui"
   ```

### 3. (Opcional) Configurar Telegram
1. Crear bot con @BotFather → obtener TOKEN
2. Obtener tu CHAT_ID (mensaje a @userinfobot)
3. Completar en `config.py`:
   ```python
   TELEGRAM_TOKEN   = "123456:ABC..."
   TELEGRAM_CHAT_ID = "987654321"
   ```

### 4. Ejecutar
```bash
python bot.py
```

---

## 🔄 Flujo de datos

```
Binance WebSocket (testnet)
    │
    ▼  (stream combinado, 20 pares simultáneos)
process_kline()
    │  acumula velas en buffer (deque 100 velas)
    │  solo actúa al cierre de cada vela
    ▼
compute_indicators()
    │  RSI(14) / MACD(12,26,9) / EMA(9,21) / BB(20)
    ▼
generate_signal()
    │  AND lógico: mínimo 3/4 confirmaciones
    ▼
try_open_position()   ←── check_exit_conditions() (SL/TP)
    │
    ▼
place_market_order()  →  Binance REST API
    │
    ▼
log_trade() + alert_trade()  →  CSV + Telegram
```

---

## 📊 Lógica de señales

| Indicador | Señal BUY              | Señal SELL              |
|-----------|------------------------|-------------------------|
| RSI(14)   | RSI < 35               | RSI > 65                |
| MACD      | Cruza al alza          | Cruza a la baja         |
| EMA 9/21  | Golden cross           | Death cross             |
| Bollinger | Precio ≤ banda inferior| Precio ≥ banda superior |

**Mínimo 3/4 para ejecutar.** Configurable en `config.py` → `MIN_CONFIRMATIONS`

---

## 💰 Gestión de riesgo

| Parámetro           | Valor default | Variable            |
|---------------------|---------------|---------------------|
| Capital por trade   | $50 USDT      | TRADE_AMOUNT_USDT   |
| Stop Loss           | -1.5%         | STOP_LOSS_PCT       |
| Take Profit         | +2.5%         | TAKE_PROFIT_PCT     |
| Posiciones máximas  | 3             | MAX_OPEN_POSITIONS  |

---

## ⚠️ Limitaciones importantes (Spot Testnet)

1. **Solo BUY en Spot** — no hay short sin margen habilitado
2. **Scalping en testnet ≠ live** — spread y latencia son diferentes
3. **SELL signals** generan señal pero no ejecutan (sin posición previa)
4. El testnet se reinicia periódicamente → balances se resetean

---

## 🚀 Siguientes pasos sugeridos

- [ ] Backtesting con datos históricos antes de pasar a real
- [ ] Ajuste de parámetros RSI/MACD por par (no todos los pares son iguales)
- [ ] Integrar el modelo Inertia (5 estados) como filtro adicional
- [ ] Dashboard web simple (Flask + Chart.js) para visualizar en tiempo real
- [ ] Paper trading con registro de P&L simulado

---

## 🛑 Parar el bot

```
Ctrl + C
```
El bot cierra todas las posiciones abiertas antes de detenerse.
