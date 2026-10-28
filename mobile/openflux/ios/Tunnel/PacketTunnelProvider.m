#import "PacketTunnelProvider.h"
@import Afzcore;
#include <sys/socket.h>

@interface PacketTunnelProvider ()
@property(nonatomic,strong) AFZMobileClient *client;
@property(nonatomic,strong) NSLock *stateLock;
@end

@implementation PacketTunnelProvider
- (instancetype)init {
    if ((self=[super init])) { _stateLock=[[NSLock alloc] init]; }
    return self;
}
- (BOOL)isCurrent:(AFZMobileClient *)client {
    [self.stateLock lock]; BOOL current=self.client==client; [self.stateLock unlock]; return current;
}
- (void)startTunnelWithOptions:(NSDictionary<NSString *,NSObject *> *)options completionHandler:(void (^)(NSError *))completionHandler {
    NETunnelProviderProtocol *protocol=(NETunnelProviderProtocol *)self.protocolConfiguration;
    NSString *document=protocol.providerConfiguration[@"document_url"];
    if (![document isKindOfClass:NSString.class] || !AFZMobileValidateURL(document)) {
        completionHandler([NSError errorWithDomain:@"AFZOpenFlux" code:1 userInfo:@{NSLocalizedDescriptionKey:@"Импортируйте профиль OpenFlux из бота."}]); return;
    }
    NSError *error=nil; AFZMobileClient *active=AFZMobileStart(document,&error);
    if (!active) { completionHandler(error); return; }
    [self.stateLock lock]; self.client=active; [self.stateLock unlock];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        // The provider's transport uses the underlying network, outside packetFlow.
        NSDate *deadline=[NSDate dateWithTimeIntervalSinceNow:30];
        while ([self isCurrent:active] && ![active connected] && [deadline timeIntervalSinceNow]>0) { [NSThread sleepForTimeInterval:0.1]; }
        if (![self isCurrent:active] || ![active connected]) {
            [self.stateLock lock]; if (self.client==active) self.client=nil; [self.stateLock unlock]; [active stop];
            completionHandler([NSError errorWithDomain:@"AFZOpenFlux" code:2 userInfo:@{NSLocalizedDescriptionKey:@"Транспорт недоступен. Проверьте сеть и профиль."}]); return;
        }
        NEPacketTunnelNetworkSettings *settings=[[NEPacketTunnelNetworkSettings alloc] initWithTunnelRemoteAddress:@"185.239.141.75"];
        settings.MTU=@1280;
        settings.IPv4Settings=[[NEIPv4Settings alloc] initWithAddresses:@[@"10.10.10.2"] subnetMasks:@[@"255.255.255.0"]];
        settings.IPv4Settings.includedRoutes=@[[NEIPv4Route defaultRoute]];
        // Capture unsupported IPv6 so it cannot silently bypass this test VPN.
        settings.IPv6Settings=[[NEIPv6Settings alloc] initWithAddresses:@[@"fd42:af2::2"] networkPrefixLengths:@[@64]];
        settings.IPv6Settings.includedRoutes=@[[NEIPv6Route defaultRoute]];
        settings.DNSSettings=[[NEDNSSettings alloc] initWithServers:@[@"1.1.1.1",@"9.9.9.9"]];
        settings.DNSSettings.matchDomains=@[@""];
        [self setTunnelNetworkSettings:settings completionHandler:^(NSError *settingsError) {
            if (settingsError || ![self isCurrent:active]) {
                [self.stateLock lock]; if (self.client==active) self.client=nil; [self.stateLock unlock]; [active stop];
                completionHandler(settingsError ?: [NSError errorWithDomain:@"AFZOpenFlux" code:3 userInfo:@{NSLocalizedDescriptionKey:@"Подключение отменено."}]); return;
            }
            [self readOutgoing:active];
            dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
                while ([self isCurrent:active]) {
                    NSError *readError=nil; NSData *packet=[active readPacket:&readError];
                    if (!packet) { if ([self isCurrent:active]) [self cancelTunnelWithError:readError]; return; }
                    [self.packetFlow writePackets:@[packet] withProtocols:@[@(AF_INET)]];
                }
            });
            completionHandler(nil);
        }];
    });
}
- (void)readOutgoing:(AFZMobileClient *)active {
    if (![self isCurrent:active]) return;
    [self.packetFlow readPacketsWithCompletionHandler:^(NSArray<NSData *> *packets,NSArray<NSNumber *> *protocols) {
        if (![self isCurrent:active]) return;
        for (NSData *packet in packets) {
            NSError *error=nil;
            if (![active sendPacket:packet error:&error]) { [self cancelTunnelWithError:error]; return; }
        }
        [self readOutgoing:active];
    }];
}
- (void)stopTunnelWithReason:(NEProviderStopReason)reason completionHandler:(void (^)(void))completionHandler {
    [self.stateLock lock]; AFZMobileClient *old=self.client; self.client=nil; [self.stateLock unlock];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{ [old stop]; completionHandler(); });
}
@end
