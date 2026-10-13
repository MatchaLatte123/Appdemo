# Fan firmware - Discovery fix

This version is based directly on `SmartHome_MQTT_Profile_Update.zip`.

## Change

The Fan firmware previously published `smarthome/device/announce` as a retained MQTT message. That caused the broker to keep the last announcement, so the App could discover `ESP32-FAN-01` even after the ESP32 Fan was powered off.

The firmware now publishes the discovery announcement as **non-retained**:

```cpp
mqttClient.publish(
    TOPIC_ANNOUNCE,
    out,
    false
);
```

`smarthome/device/register` remains non-retained as before.

No existing Fan functions were removed or rewritten: profile/NVS, scenario modes, temperature/schedule logic, MQTT control, security/quarantine, telemetry, config ACK and reset-profile behavior are preserved.

## One-time retained-message cleanup

If the old retained Fan announcement is already stored in Mosquitto, changing the firmware alone does not remove it. Clear it once from the MQTT broker:

```powershell
mosquitto_pub -h 192.168.1.130 -p 1883 -t "smarthome/device/announce" -n -r
```

Then power the Fan ESP32 on again. Its live announcement will be sent normally while it is online, but the broker will no longer keep an announcement after shutdown.
