# Patrones de derrota

En 291 partidas se sostuvieron asociaciones entre el estado al minuto 14, las muertes propias y el resultado; las causas atribuidas al video no se replicaron con fiabilidad.

## Pregunta y muestra

La pregunta fue qué distingue las derrotas de las victorias del jugador y si las primeras se explicaban por el MMR de aliados, sus rachas, el fin de semana o el campeón. La muestra incluye 291 partidas de solo queue (cola 420), sin remakes, entre el 19 de mayo y el 25 de octubre de 2026: 227 de la cuenta principal, cohorte `master`, y 64 de la cuenta secundaria, cohorte `emerald`. Hubo 121 derrotas y 170 victorias, con 1796 muertes propias. Casi todas las partidas fueron con Sona soporte.

De las 291 partidas, 272 tienen grabación completa; 19 no tienen video y 3 videos no pudieron calibrarse por recorte o daño. La muestra de video de la fase 2 fue de 49 partidas.

| Cohorte | n partidas | Derrotas | Victorias |
|---|---:|---:|---:|
| master | 227 | 99 | 128 |
| emerald | 64 | 22 | 42 |
| Total | 291 | 121 | 170 |

## Fase 1 — hipótesis iniciales del jugador

El análisis inicial comparó 10 derrotas seguidas con 10 victorias de control y después examinó la muestra disponible. Para las derrotas, el rango actual medio del equipo propio, contando al jugador, fue 1148 LP sobre Master frente a 1158 del rival: similares. Esos rangos describen el momento actual, no el rango al jugar esas partidas.

En las derrotas, los aliados y rivales tenían ambos 56% de victorias en sus cinco partidas anteriores. Llegaban con tres o más derrotas seguidas 3/40 aliados y 5/50 rivales. Estos datos no apoyaron la hipótesis de una “losers queue”.

El fin de semana tuvo 49% de victorias (n = 82 partidas), frente a 62% entre semana (n = 209; p≈0,04). Se miraron varios cortes —día, hora y posición en la sesión—, por lo que es un indicio, no una conclusión. No permite separar la calidad de los compañeros del posible cambio en el juego del jugador.

Las fuentes públicas situaban al campeón en 51–53% de victorias en el parche y en tier alto de Master+. Las cinco derrotas del parche correspondían a la racha observada, no demostraban un efecto del parche.

En las 10 derrotas iniciales sí cambió la frecuencia de muerte: 3,9 muertes por 10 minutos, frente a 2,7 en derrotas anteriores y 1,9 en victorias. Siete de nueve derrotas tuvieron cinco o más muertes propias antes del minuto 14.

## Fase 2 — etiquetado a ciegas en video

El etiquetador A (el analista) revisó 141 muertes anteriores al minuto 14 y 189 desde el minuto 14 en 49 partidas. Para cada muerte se presentaron cuatro fotogramas, tomados a −14, −8, −4 y −1 segundos. Los archivos tenían nombres aleatorios y el resultado de la partida se desvelaba después de etiquetar.

El resultado aparente fue una tasa de muertes por roam o gank del mid o jungla rival antes del 14 de 0,79 por partida en derrotas frente a 0,31 en victorias. De las 27 muertes con esa etiqueta, 19 ocurrieron en derrotas, cuando se esperaban unas 13 si la etiqueta fuera igual de probable por partida (prueba binomial p≈0,016). Desde el 14, el 21% de las muertes en derrotas y el 9% en victorias se etiquetaron como muertes en objetivos.

De ese resultado surgió la regla “con −3000 de oro no se pelean objetivos” y una forma de estimar el déficit de oro en partida con señales visibles. Ambas se contrastaron después con etiquetadores adicionales y datos de eventos.

## Fase 3 — fiabilidad y replicación

### Acuerdo entre etiquetadores y control por reloj

Sobre 40 muertes ya etiquetadas por A, el segundo etiquetador independiente coincidió en 65%. Tras afinar los criterios de desempate, el acuerdo volvió a ser 65%. El tercer etiquetador coincidió en 58%; el acuerdo entre el segundo y el tercero fue 55%. En las mismas 20 muertes tempranas, la etiqueta “roam antes del 14” apareció en 2 casos para A, 4 para el segundo y 7 para el tercero.

Cada etiqueta debía incluir el reloj de partida leído en el último fotograma. Se comparó con el momento real de la muerte y se descartó si la diferencia excedía ±4 segundos. Este control garantizaba que cada etiqueta correspondiera a una imagen efectivamente revisada. En total se rechazaron 140 etiquetas por este control.

Se etiquetaron y validaron con el segundo etiquetador 460 muertes nuevas en partidas distintas a las de la fase 2. Otras 892 quedaron sin etiquetar: la composición ya no diferenciaba las causas aparentes y la fiabilidad limitaba el valor de añadir más etiquetas.

### Lo que no se sostuvo

Las proporciones siguientes son la parte de las muertes atribuida a cada causa en derrotas frente a victorias; los intervalos son IC 95% por bootstrap de partidas.

| Etiqueta | Etiquetador A | Segundo etiquetador independiente | Todas las etiquetas |
|---|---|---|---|
| Roam antes del 14 | 26% vs 12%; diferencia +13,7 puntos [ +0,9, +30,4 ] | 16% vs 25%; diferencia −8,9 [ −18,5, +1,2 ] | 20% vs 20% |
| Objetivo desde el 14 | 21% vs 9%; diferencia +11,7 [ +1,4, +21,3 ] | 11% vs 17%; diferencia −6,6 [ −15,8, +2,6 ] | 16% vs 14% |

El efecto temprano de roam/gank cambió de dirección con el segundo etiquetador y se anuló al reunir las etiquetas. La diferencia aparente en muertes en objetivos también cambió de dirección con el segundo etiquetador; con todas las etiquetas, la diferencia fue pequeña.

La comprobación con eventos de Riot, sin interpretación manual y sobre 291 partidas, tampoco reprodujo una diferencia concluyente. Antes del 14, las muertes por 10 minutos en bot con mid, jungla o top rival implicado fueron 0,82 en derrotas y 0,65 en victorias; diferencia +0,17 [−0,00, +0,35], no significativa. Desde el 14, las muertes por 10 minutos a distancia de foso con un objetivo caído dentro de ±45 segundos fueron 0,43 y 0,34; diferencia +0,08 [−0,04, +0,21], no significativa.

Con todas las etiquetas, las únicas categorías distintas fueron defender torre (12% vs 3%) y empujón final (6% vs 2%); ambas son compatibles con consecuencias de ir perdiendo, no con causas demostradas.

## Lo que sí se sostuvo

Las siguientes asociaciones se reproducen en el informe generado por `scripts/loss_patterns.py`. Son observaciones de esta muestra, no reglas causales.

### Victoria por oro del equipo al minuto 14

n = 291 partidas. IC Wilson 95%. En 4 partidas no hubo frame del minuto 14 o terminaron antes; para ellas se usó el último frame disponible, que no equivale a una observación real al 14.

| Grupo | Victorias | n | Victoria | IC 95% |
|---|---:|---:|---:|---:|
| +2000+ | 66 | 72 | 91.7% | 83.0% a 96.1% |
| 0..+2000 (sin +2000) | 49 | 63 | 77.8% | 66.1% a 86.3% |
| -2000..0 (sin 0) | 39 | 70 | 55.7% | 44.1% a 66.8% |
| -4000..-2000 (sin -2000) | 12 | 44 | 27.3% | 16.3% a 41.8% |
| < -4000 | 4 | 42 | 9.5% | 3.8% a 22.1% |

### Victoria por muertes propias antes del minuto 14

n = 291 partidas. IC Wilson 95%.

| Grupo | Victorias | n | Victoria | IC 95% |
|---|---:|---:|---:|---:|
| 0-1 | 51 | 74 | 68.9% | 57.7% a 78.3% |
| 2 | 38 | 54 | 70.4% | 57.2% a 80.9% |
| 3 | 34 | 62 | 54.8% | 42.5% a 66.6% |
| 4 | 20 | 42 | 47.6% | 33.4% a 62.3% |
| 5+ | 27 | 59 | 45.8% | 33.7% a 58.3% |

### Victoria por muertes encadenadas antes del minuto 14

n = 291 partidas. IC Wilson 95%. Una muerte encadenada ocurrió a ≤96 segundos de la muerte propia anterior; el corte es estricto antes del 14.

| Grupo | Victorias | n | Victoria | IC 95% |
|---|---:|---:|---:|---:|
| 0 | 116 | 180 | 64.4% | 57.2% a 71.1% |
| 1 | 33 | 60 | 55.0% | 42.5% a 66.9% |
| 2+ | 21 | 51 | 41.2% | 28.8% a 54.8% |

### Tasas de muertes propias por 10 minutos

n = 121 derrotas y 170 victorias. Las ventanas son [inicio, fin); IC bootstrap percentil 95%, 2000 remuestreos por partida (derrotas y victorias por separado, sin estratificar por cohorte), semilla 1. Cada ventana remuestrea solo partidas con exposición positiva.

| Señal | n muertes D/V | Minutos D/V | Tasa D | Tasa V | D - V | IC 95% diferencia |
|---|---:|---:|---:|---:|---:|---:|
| Antes del 14 | 405/442 | 1680.0/2375.4 | 2.411 | 1.861 | 0.550 | 0.257 a 0.850 |
| Encadenadas antes del 14 | 103/85 | 1680.0/2375.4 | 0.613 | 0.358 | 0.255 | 0.072 a 0.449 |
| Desde el 14 | 523/426 | 1759.1/2387.6 | 2.973 | 1.784 | 1.189 | 0.896 a 1.486 |
| Desde el 14 en mitad propia | 396/193 | 1759.1/2387.6 | 2.251 | 0.808 | 1.443 | 1.172 a 1.714 |

La mitad propia se define como x+y<15000 para el equipo 100 y x+y>15000 para el equipo 200; se excluye la diagonal. Las muertes tardías sin posición excluidas del numerador posicional fueron 0. El denominador es el tiempo jugado en la ventana, no el tiempo en esa mitad.

### Riesgo de muerte cerca del foso por banda de oro

La unidad n es el número de eventos de dragón, barón o heraldo desde el minuto 14. El oro corresponde al frame del minuto del evento. Se cuenta muerte propia a ≤3000 unidades de la posición del objetivo, entre 60 segundos antes y 30 segundos después, con bordes incluidos.

| Oro de equipo | n objetivos | Tomó el rival | Muerte cerca del foso |
|---|---:|---:|---:|
| >= +3000 | 360 | 15.6% | 9.4% |
| (-3000, +3000) | 548 | 47.4% | 18.8% |
| (-6000, -3000] | 181 | 70.7% | 16.0% |
| <= -6000 | 180 | 87.8% | 10.6% |

No hubo objetivos evaluados sin coordenadas ni muertes propias sin coordenadas en la muestra. El valor exacto −6000 pertenece a la última banda; la banda intermedia es (−6000,−3000]. Un evento cuenta como máximo una muerte aunque haya varias. Las posiciones ausentes pueden subestimar la proporción; los eventos de una misma partida no son independientes. La ventana incluye información posterior, así que no demuestra predicción en vivo.

El jugador murió menos cerca del foso por evento cuando su equipo iba por detrás (16,0% y 10,6%) que con la partida igualada (18,8%); los datos no apoyan la regla simple “con −3000 de oro no se pelean objetivos”.

### Reglas visibles para estimar déficit de oro

n = 4298 observaciones partida-minuto desde el 14 hasta el último minuto entero con frames. El objetivo es detectar oro de equipo ≤−3000. Precisión = verdaderos positivos / minutos marcados; recall = verdaderos positivos / minutos con déficit.

| Regla | Precision | Recall | n minutos marcados |
|---|---:|---:|---:|
| kills abajo >=5 | 71.8% | 85.3% | 1479 |
| torres abajo >=2 | 77.8% | 75.3% | 1205 |
| niveles abajo >=3 | 82.3% | 74.2% | 1123 |
| 2 de 3 | 86.2% | 84.1% | 1215 |

Las partidas largas aportan más minutos y las observaciones no son independientes. Si falta denominador, se muestra “sin datos”. No hay validación fuera de muestra ni control por parche, campeón o cohorte.

## Lecciones de método

1. Un hallazgo aparente en 49 partidas y con un etiquetador no debe guiar una decisión antes de replicarse; aquí no sobrevivió al segundo etiquetador ni a la medición automática en 291 partidas.
2. La clasificación de muertes por fotogramas tuvo 55–65% de acuerdo. Conviene priorizar métricas de eventos y usar el video como apoyo.
3. La validación por reloj es barata y detecta etiquetas que no corresponden a la imagen revisada.
4. Todo es correlación. Parte de las muertes tempranas puede ser consecuencia de que el equipo ya va por detrás.

## Implicación para el producto

`objective_readiness_v1` propone ceder según el estado de la partida. En esta muestra no aparece mayor riesgo del jugador de morir en el foso cuando va por detrás; con déficit de oro, la tasa observada es menor que con la partida igualada. Esta evidencia se anota contra ese supuesto y no produce cambios en el producto por ahora.

## Límites

- Los resultados son correlaciones, no causas; parte de las muertes puede ocurrir después de que la partida ya se inclinó.
- La muestra corresponde a un solo jugador.
- El rango y la forma de los otros jugadores se midieron en el momento actual, no en el momento de las partidas analizadas.
- Las etiquetas de video tuvieron fiabilidad de 55–65% de acuerdo.
- No hay validación fuera de muestra.
- La tabla de riesgo en el foso usa una ventana que incluye información posterior al evento y no demuestra predicción en vivo; los eventos de una misma partida no son independientes.
