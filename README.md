# aidmx02

## Baslama

3. Ana penceredeki bant butonlarini (20-60 Hz Monitor, 60-200 Hz Monitor, 200-600 Hz Monitor, 600-2 kHz Monitor, 2-6 kHz Monitor, 6-20 kHz Monitor) kullanarak istediginiz frekans penceresini acin; bar alti Min RMS / Max RMS alanlari bandin olcek araligini ayarlamaniza yardimci olur.
4. Ayrik pencerelerdeki beat gostergesi sinirli veriyle egitilmis kucuk bir RNN modelinden beslenir; kirmizi LED ve Beat: etiketi olasi beat darbelerini yansitir.
3. 20-60 Hz bandindaki RMS dalgalanmasini ayrica gormek icin ana penceredeki `20-60 Hz Monitor` tusuna basin; yeni pencerede cift yonlu bar guncellenir.
4. Ayrı penceredeki beat göstergesi sinirli veriyle eğitilmiş küçük bir RNN modelinden beslenir; kırmızı LED ve `Beat:` etiketi olası beat darbelerini yansıtır. Alt kısımdaki `Min RMS` / `Max RMS` alanlarıyla barın ölçek aralığını ayarlayabilirsiniz.

## Sorun Giderme

- Hata alirsaniz proje klasorundaki `loopback_monitor.log` dosyasini kontrol ederek ayrintilari gorebilirsiniz.