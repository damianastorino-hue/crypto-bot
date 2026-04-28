import React, { useState, useEffect } from 'react';
import {
  StyleSheet,
  View,
  TouchableOpacity,
  Text,
  SafeAreaView,
  StatusBar,
} from 'react-native';
import CryptoBot from './app/CryptoBot';

export default function App() {
  const [isRunning, setIsRunning] = useState(false);
  const [botInstance, setBotInstance] = useState(null);

  useEffect(() => {
    if (isRunning && !botInstance) {
      const bot = new CryptoBot();
      bot.start();
      setBotInstance(bot);
    } else if (!isRunning && botInstance) {
      botInstance.pause();
      setBotInstance(null);
    }
  }, [isRunning]);

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#1a1a2e" />
      
      <View style={styles.content}>
        {/* Logo/Title */}
        <View style={styles.header}>
          <Text style={styles.title}>🤖 CRYPTO BOT</Text>
          <Text style={styles.subtitle}>v0.8.1 Mobile</Text>
        </View>

        {/* Status */}
        <View style={styles.statusBox}>
          <View style={[styles.statusIndicator, { backgroundColor: isRunning ? '#00FF00' : '#FF0000' }]} />
          <Text style={styles.statusText}>
            {isRunning ? 'RUNNING' : 'STOPPED'}
          </Text>
        </View>

        {/* Button */}
        <TouchableOpacity
          style={[styles.button, { backgroundColor: isRunning ? '#FF6B6B' : '#4CAF50' }]}
          onPress={() => setIsRunning(!isRunning)}
        >
          <Text style={styles.buttonText}>
            {isRunning ? 'PAUSE' : 'START'}
          </Text>
        </TouchableOpacity>

        {/* Footer */}
        <View style={styles.footer}>
          <Text style={styles.footerText}>Bot corriendo en background</Text>
          <Text style={styles.footerText}>Monitorea 20 tickers</Text>
          <Text style={styles.footerText}>Timeout: 30 min</Text>
        </View>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#1a1a2e',
  },
  content: {
    flex: 1,
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: 60,
  },
  header: {
    alignItems: 'center',
  },
  title: {
    fontSize: 48,
    fontWeight: 'bold',
    color: '#00FF00',
    marginBottom: 5,
  },
  subtitle: {
    fontSize: 16,
    color: '#888',
  },
  statusBox: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#16213e',
    paddingHorizontal: 30,
    paddingVertical: 20,
    borderRadius: 10,
    marginVertical: 40,
  },
  statusIndicator: {
    width: 20,
    height: 20,
    borderRadius: 10,
    marginRight: 15,
  },
  statusText: {
    fontSize: 24,
    fontWeight: 'bold',
    color: '#FFF',
    letterSpacing: 2,
  },
  button: {
    width: 200,
    height: 200,
    borderRadius: 100,
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: 40,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.3,
    shadowRadius: 4,
    elevation: 5,
  },
  buttonText: {
    fontSize: 32,
    fontWeight: 'bold',
    color: '#FFF',
    letterSpacing: 1,
  },
  footer: {
    alignItems: 'center',
  },
  footerText: {
    color: '#888',
    fontSize: 12,
    marginVertical: 3,
  },
});
