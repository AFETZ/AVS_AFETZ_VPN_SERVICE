package uk.superbebra.afzopenflux;

import android.app.*;
import android.content.Intent;
import android.net.VpnService;
import android.os.ParcelFileDescriptor;
import java.io.*;
import java.util.Arrays;
import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import mobile.Client;
import mobile.Mobile;

public final class TunnelService extends VpnService {
    public static volatile boolean running=false;
    public static volatile String statusText="Отключено";
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private volatile Client client;
    private ParcelFileDescriptor tunnel;
    private FileInputStream reader;
    private FileOutputStream writer;

    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent==null || "stop".equals(intent.getAction())){worker.execute(()->{disconnect();stopSelf();});return START_NOT_STICKY;}
        String url=intent.getStringExtra("document_url");
        if(url==null || !Mobile.validateURL(url)){stopSelf();return START_NOT_STICKY;}
        NotificationManager manager=getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("vpn","Подключение OpenFlux",NotificationManager.IMPORTANCE_LOW));
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent stop=PendingIntent.getService(this,1,new Intent(this,TunnelService.class).setAction("stop"),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        Notification note=new Notification.Builder(this,"vpn").setSmallIcon(R.drawable.ic_vpn).setContentTitle("AFZ OpenFlux").setContentText("Тестовое VPN-подключение").setContentIntent(open).setOngoing(true).addAction(new Notification.Action.Builder(null,"Отключить",stop).build()).build();
        startForeground(1,note);running=true;statusText="Подключение…";
        worker.execute(()->connect(url));return START_NOT_STICKY;
    }

    private void connect(String url){
        if(client!=null)return;
        try{
            // Exclude this transport-only app, including its bootstrap DNS, to avoid loops.
            // All other apps remain routed into the TUN; IPv6 is blocked by VpnService.
            tunnel=new Builder().setSession("AFZ OpenFlux").setMtu(1280).addAddress("10.10.10.2",24)
                .addRoute("0.0.0.0",0).addDnsServer("1.1.1.1").addDnsServer("9.9.9.9")
                .addDisallowedApplication(getPackageName()).setBlocking(true).establish();
            if(tunnel==null)throw new IOException("VPN permission revoked");
            reader=new FileInputStream(tunnel.getFileDescriptor());writer=new FileOutputStream(tunnel.getFileDescriptor());
            Client active=Mobile.start(url);client=active;running=true;
            new Thread(()->{
                byte[] buffer=new byte[65535];
                try{while(client==active){int n=reader.read(buffer);if(n<0)break;active.sendPacket(Arrays.copyOf(buffer,n));}}
                catch(Exception ignored){}finally{if(client==active)stopSelf();}
            },"OpenFlux-TUN-read").start();
            new Thread(()->{
                try{while(client==active){byte[] packet=active.readPacket();writer.write(packet);}}
                catch(Exception ignored){}finally{if(client==active)stopSelf();}
            },"OpenFlux-TUN-write").start();
            new Thread(()->{
                while(client==active){
                    statusText=active.connected() ? "Подключено · получено "+(active.receivedBytes()/1024)+" КБ" : "Подключение / восстановление связи…";
                    try{Thread.sleep(1000);}catch(InterruptedException e){return;}
                }
            },"OpenFlux-status").start();
        }catch(Exception error){disconnect();statusText="Не удалось запустить VPN. Повторите подключение.";stopSelf();}
    }

    private void disconnect(){
        Client old=client;client=null;running=false;
        if(old!=null)old.stop();
        try{if(reader!=null)reader.close();}catch(IOException ignored){}
        try{if(writer!=null)writer.close();}catch(IOException ignored){}
        try{if(tunnel!=null)tunnel.close();}catch(IOException ignored){}
        tunnel=null;reader=null;writer=null;statusText="Отключено";
        stopForeground(STOP_FOREGROUND_REMOVE);
    }
    @Override public void onRevoke(){stopSelf();super.onRevoke();}
    @Override public void onDestroy(){worker.execute(this::disconnect);worker.shutdown();super.onDestroy();}
}
