import axios from 'axios';
import BackgroundTimer from 'react-native-background-timer';

// API Keys - REEMPLAZA CON LAS TUYAS
const BINANCE_API_KEY = 'TU_API_KEY';
const BINANCE_API_SECRET = 'TU_API_SECRET';

// Configuración
const CAPITAL_POR_SLOT = 50; // USDT
const TIMEOUT_MINUTOS = 30;
const CICLO_ESPERA = 300; // 5 minutos

const TOP_20_TICKERS = [
  'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
  'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'UNIUSDT',
  'LTCUSDT', 'FILUSDT', 'ATOMUSDT', 'APTUSDT', 'SUIUSDT',
  'AAVEUSDT', 'MANAUSDT', 'INJUSDT', 'SANDUSDT', 'NEARUSDT',
];

class CryptoBot {
  constructor() {
    this.isRunning = false;
    this.isPaused = false;
    this.operacionesActivas = { slot_1: null, slot_2: null };
    this.simboloCache = {};
    this.cicloInterval = null;
  }

  // ==================== UTILIDADES ====================

  calcularSigmaSimple(closes) {
    if (closes.length < 2) return 0.0001;
    const media = closes.reduce((a, b) => a + b) / closes.length;
    const varianza =
      closes.reduce((sum, x) => sum + Math.pow(x - media, 2), 0) /
      closes.length;
    return Math.max(Math.sqrt(varianza), 0.0001);
  }

  detectarCruce(closes) {
    if (closes.length < 3) return false;
    const sigma = this.calcularSigmaSimple(closes);
    const cambio1 = closes[closes.length - 2] - closes[closes.length - 3];
    const cambio2 = closes[closes.length - 1] - closes[closes.length - 2];
    return cambio1 < -sigma && cambio2 > sigma;
  }

  detectarMaximoLocal(closes) {
    if (closes.length < 3) return false;
    const sigma = this.calcularSigmaSimple(closes);
    const cambio1 = closes[closes.length - 2] - closes[closes.length - 3];
    const cambio2 = closes[closes.length - 1] - closes[closes.length - 2];
    return cambio1 > sigma && cambio2 < -sigma;
  }

  // ==================== API BINANCE ====================

  async obtenerKlines(ticker) {
    try {
      const response = await axios.get('https://api.binance.com/api/v3/klines', {
        params: {
          symbol: ticker,
          interval: '5m',
          limit: 50,
        },
        timeout: 10000,
      });

      if (!response.data || response.data.length === 0) {
        return null;
      }

      return response.data.map(k => ({
        close: parseFloat(k[4]),
      }));
    } catch (error) {
      console.log(`Error obtener klines ${ticker}:`, error.message);
      return null;
    }
  }

  async obtenerPrecio(ticker) {
    try {
      const response = await axios.get('https://api.binance.com/api/v3/ticker/price', {
        params: { symbol: ticker },
        timeout: 10000,
      });
      return parseFloat(response.data.price);
    } catch (error) {
      console.log(`Error precio ${ticker}:`, error.message);
      return null;
    }
  }

  async obtenerBalance(asset) {
    try {
      // Nota: Esta llamada requiere firma. Para simplificar en mobile,
      // podrías usar solo precio actual sin balance real.
      // Por ahora retorna valores dummy.
      return { free: 1000, locked: 0 };
    } catch (error) {
      return { free: 0, locked: 0 };
    }
  }

  // ==================== COMPRA/VENTA ====================

  async comprar(ticker, capital) {
    try {
      const precio = await this.obtenerPrecio(ticker);
      if (!precio) return null;

      // Simulación: en producción necesitarías firmar con HMAC-SHA256
      // Por ahora, log de intención
      console.log(`🟢 COMPRA: ${ticker} @ $${precio}`);

      return {
        ticker,
        asset: ticker.replace('USDT', ''),
        precioEntrada: precio,
        cantidad: capital / precio,
        timestamp: new Date(),
      };
    } catch (error) {
      console.log(`Error comprar ${ticker}:`, error.message);
      return null;
    }
  }

  async vender(ticker, precioEntrada) {
    try {
      const precio = await this.obtenerPrecio(ticker);
      if (!precio) return null;

      const pnlPct = ((precio - precioEntrada) / precioEntrada) * 100;
      const pnlUsd = (pnlPct / 100) * CAPITAL_POR_SLOT;

      console.log(`🔴 VENTA: ${ticker} @ $${precio} | P&L: ${pnlUsd.toFixed(2)} (${pnlPct.toFixed(2)}%)`);

      return {
        precioVenta: precio,
        pnlPct,
        pnlUsd,
      };
    } catch (error) {
      console.log(`Error vender ${ticker}:`, error.message);
      return null;
    }
  }

  // ==================== LÓGICA PRINCIPAL ====================

  async buscarCruceYAbrirSlot(slotName) {
    console.log(`\n🔍 [${slotName}] Buscando cruces...`);

    const tiempoMaximo = Date.now() + 5 * 60 * 1000; // 5 minutos

    for (let i = 0; i < TOP_20_TICKERS.length; i++) {
      if (Date.now() > tiempoMaximo) {
        console.log('⏱️ TIMEOUT búsqueda');
        break;
      }

      if (this.isPaused) break;

      const ticker = TOP_20_TICKERS[i];

      // No compres el mismo ticker en otro slot
      const otroSlot = slotName === 'slot_1' ? 'slot_2' : 'slot_1';
      if (
        this.operacionesActivas[otroSlot] &&
        this.operacionesActivas[otroSlot].ticker === ticker
      ) {
        continue;
      }

      const klines = await this.obtenerKlines(ticker);
      if (!klines || klines.length < 3) {
        continue;
      }

      const closes = klines.map(k => k.close);

      if (this.detectarCruce(closes)) {
        const precio = await this.obtenerPrecio(ticker);
        if (precio) {
          console.log(`🔴 ¡CRUCE DETECTADO: ${ticker} @ $${precio}`);

          const compra = await this.comprar(ticker, CAPITAL_POR_SLOT);
          if (compra) {
            this.operacionesActivas[slotName] = compra;
            return true;
          }
        }
      }

      // Pequeña pausa entre tickers
      await this.sleep(100);
    }

    return false;
  }

  async monitorearSlot(slotName) {
    const op = this.operacionesActivas[slotName];
    if (!op) return false;

    const ticker = op.ticker;
    const tiempoAbierto = (Date.now() - op.timestamp.getTime()) / 1000 / 60; // minutos

    console.log(`\n📊 [${slotName}] ${ticker}`);
    console.log(`  Tiempo: ${Math.floor(tiempoAbierto)} min`);

    const klines = await this.obtenerKlines(ticker);
    if (!klines || klines.length < 1) {
      return false;
    }

    const closes = klines.map(k => k.close);
    const precioActual = closes[closes.length - 1];
    const pnlPct = ((precioActual - op.precioEntrada) / op.precioEntrada) * 100;

    let debeVender = false;
    let razon = null;

    // Condiciones de venta
    if (this.detectarMaximoLocal(closes) && pnlPct > 0) {
      debeVender = true;
      razon = 'MÁXIMO LOCAL';
    } else if (tiempoAbierto >= TIMEOUT_MINUTOS) {
      debeVender = true;
      razon = 'TIMEOUT';
    } else if (pnlPct <= -2.0) {
      debeVender = true;
      razon = 'STOP LOSS';
    }

    if (debeVender) {
      console.log(`📉 VENDIENDO: ${razon}`);
      const venta = await this.vender(ticker, op.precioEntrada);

      if (venta) {
        this.operacionesActivas[slotName] = null;
        return true;
      }
    }

    return false;
  }

  async ejecutarCiclo() {
    if (this.isPaused) return;

    const timestamp = new Date().toLocaleTimeString();
    console.log(`\n[${timestamp}] CICLO`);
    console.log('=====================================');

    // Buscar cruces en slots vacíos
    if (!this.operacionesActivas.slot_1) {
      await this.buscarCruceYAbrirSlot('slot_1');
      if (this.operacionesActivas.slot_1) {
        await this.sleep(500);
      }
    }

    if (!this.operacionesActivas.slot_2 && this.operacionesActivas.slot_1) {
      await this.buscarCruceYAbrirSlot('slot_2');
      if (this.operacionesActivas.slot_2) {
        await this.sleep(500);
      }
    }

    // Monitorear slots activos
    if (this.operacionesActivas.slot_1) {
      await this.monitorearSlot('slot_1');
    } else {
      console.log('\n📊 [slot_1] DISPONIBLE');
    }

    if (this.operacionesActivas.slot_2) {
      await this.monitorearSlot('slot_2');
    } else {
      console.log('\n📊 [slot_2] DISPONIBLE');
    }

    console.log(`\n⏳ Esperando ${CICLO_ESPERA}s...`);
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  // ==================== CONTROLES ====================

  start() {
    if (this.isRunning) return;

    this.isRunning = true;
    this.isPaused = false;
    this.operacionesActivas = { slot_1: null, slot_2: null };

    console.log('\n🚀 BOT INICIADO');
    console.log('=====================================');

    // Ejecuta ciclo inmediatamente y luego cada CICLO_ESPERA segundos
    this.ejecutarCiclo();

    this.cicloInterval = BackgroundTimer.setInterval(() => {
      if (!this.isPaused) {
        this.ejecutarCiclo();
      }
    }, CICLO_ESPERA * 1000);
  }

  pause() {
    this.isPaused = true;
    if (this.cicloInterval) {
      BackgroundTimer.clearInterval(this.cicloInterval);
      this.cicloInterval = null;
    }
    console.log('\n⏸️ BOT PAUSADO');
  }

  resume() {
    this.isPaused = false;
    console.log('\n▶️ BOT REANUDADO');
    this.start();
  }

  stop() {
    this.isRunning = false;
    this.isPaused = true;
    if (this.cicloInterval) {
      BackgroundTimer.clearInterval(this.cicloInterval);
      this.cicloInterval = null;
    }
    console.log('\n⛔ BOT DETENIDO');
  }
}

export default CryptoBot;
