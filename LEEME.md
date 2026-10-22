# Paso Largo — control de asistencia con terminal facial (ISAPI)

Programa web para administrar terminales de reconocimiento facial Hikvision (serie DS-K1T / MinMoe)
por ISAPI y llevar el control de asistencia del personal.

## Cómo iniciarlo

Requisito: Python 3.10 o superior (en Windows, al instalarlo marca **"Add Python to PATH"**).

- **Windows:** doble clic en `iniciar.bat`. La primera vez instala lo necesario (1–2 minutos).
- **Linux / Mac:** `./iniciar.sh`

Luego abre **http://localhost:8000** — usuario `admin`, contraseña `admin123` (cámbiala en *Mi cuenta*).
Otras PCs de la misma red pueden entrar con `http://IP-DE-ESTA-PC:8000` (permite el puerto 8000 en el firewall).

La PC donde corre debe estar en la misma red que el terminal (por ejemplo 192.168.150.x) y quedar encendida,
porque es la que lee las marcaciones cada 10 segundos.

## Primeros pasos

1. **Dispositivos → Agregar equipo:** IP `192.168.150.231`, puerto 80, usuario `admin` y la contraseña del equipo.
   Ajusta la zona horaria (por ejemplo `-05:00` para Perú/Colombia/Ecuador, `-06:00` México, `-03:00` Argentina/Chile).
   Al guardar se prueba la conexión y se muestra modelo, serie y firmware.
2. **Horarios y ajustes:** crea horarios (entrada, salida, tolerancia, refrigerio, días laborables), departamentos y feriados.
3. **Empleados:** créalos con su foto (o con la cámara de la PC). Al guardar se envían al equipo con su rostro.
   Si ya tenías gente registrada en el equipo, usa **Importar → Desde un equipo**. También se puede importar un Excel.
4. Botón **Eventos** en el dispositivo: descarga el historial de marcaciones (por defecto 31 días).

## Qué incluye

- **Panel** en vivo: presentes, tardanzas, ausentes, gráfico de 7 días, últimas marcaciones, estado de equipos y botón de abrir puerta.
- **Asistencia del día** con entrada, salida, tardanza, horas trabajadas y estado; **marcas manuales** para olvidos (quedan auditadas).
- **Marcaciones**: todos los eventos del equipo con filtros y la foto capturada en cada marca.
- **Reportes** por rango, empleado o departamento: resumen (días, tardanzas, faltas, horas, horas extra) y detalle diario; **exportación a Excel y PDF**.
- **Permisos, vacaciones y licencias** que justifican faltas.
- **Varios equipos**: cada uno puede ser de entrada, salida o ambos.
- **Control de puerta**: abrir, mantener abierta, bloquear, volver a normal.
- **Sincronizar hora**, reiniciar equipo, probar conexión.
- **Usuarios con roles** (Administrador, Supervisor, Consulta) y **auditoría** de todas las acciones.

## Cómo se calcula la asistencia

- Entrada = primera marca del día; salida = última (si hay al menos 2 minutos de diferencia, configurable).
  Si el equipo está configurado como *solo entrada* o *solo salida*, o si el empleado elige Entrada/Salida en el terminal, se respeta.
- Tardanza = entrada después de *hora de inicio + tolerancia*. Se reportan los minutos desde la hora de inicio.
- El refrigerio se descuenta de las horas trabajadas cuando la jornada supera 5 horas.
- Falta = día laborable sin marcas, que no es feriado ni tiene permiso.
- Turnos que cruzan la medianoche no están soportados todavía.

## Datos

Todo se guarda en la carpeta `datos/` (base SQLite `asistencia.db` y fotos). **Haz copia de esa carpeta** para respaldar.
La contraseña del terminal se guarda en esa base, así que protege el acceso a la PC.

## Probar sin el equipo

`python simulador.py` levanta un terminal simulado en `127.0.0.1:8090` (usuario `admin`, clave `12345`)
que genera marcaciones al azar. Agrégalo como dispositivo para ver el sistema funcionando.

## Problemas comunes

- **401 / usuario o contraseña incorrectos:** revisa la clave; tras varios intentos fallidos el equipo bloquea la IP unos minutos.
- **No se pudo conectar:** verifica que la PC haga `ping 192.168.150.231` y que ISAPI/HTTP esté habilitado
  (en la web del equipo: *Configuración → Red → Servicios de red*, o en SADP).
- **Rostro rechazado:** la foto debe mostrar un solo rostro de frente, bien iluminado; prueba con otra foto.
- **Horas de las marcas desfasadas:** usa *Sincronizar hora* y revisa la zona horaria del dispositivo.

## Despliegue en Dokploy (Docker)

1. En Dokploy crea un servicio **Compose** (o **Application** con build type *Dockerfile*) apuntando a este repositorio, rama `main`.
2. En **Domains** asigna tu dominio al puerto **8000** (servicio `web`).
3. Despliega. Los datos (base de datos, fotos y clave de sesión) quedan en el volumen `paso-largo-datos` montado en `/data`, así que no se pierden al volver a desplegar.
4. Entra con `admin / admin123` y cambia la contraseña de inmediato.

**Importante:** el servidor de Dokploy tiene que poder llegar al terminal Hikvision (por ejemplo `192.168.1.19`).
Si Dokploy está en un VPS en internet, no verá la red local del terminal: hace falta una VPN (WireGuard, Tailscale)
entre el VPS y la red del terminal, o instalar Dokploy en una PC/servidor dentro de esa misma red.

La zona horaria se define con la variable `TZ` (por defecto `America/Caracas`). Usa una sola réplica: el lector de eventos del equipo no debe duplicarse.
