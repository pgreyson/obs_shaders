// sidecar — list / connect / disconnect Sidecar iPads from the command line.
// macOS has no setting to reconnect Sidecar after a restart; this calls the same private
// SidecarCore framework the Displays menu uses, so a login item can do it.
//   sidecar list                        reachable iPads (* = connected)
//   sidecar connect [NAME] [--wait S]   connect NAME (or the first iPad); keep trying for S seconds
//   sidecar disconnect [NAME]
// Build: clang -fobjc-arc -framework Foundation sidecar.m -o sidecar
#import <Foundation/Foundation.h>
#import <dlfcn.h>

@interface NSObject (SidecarPrivate)
+ (id)sharedManager;
- (NSArray *)devices;
- (NSArray *)connectedDevices;
- (NSString *)name;
- (void)connectToDevice:(id)d completion:(void (^)(NSError *))done;
- (void)disconnectFromDevice:(id)d completion:(void (^)(NSError *))done;
@end

static NSError *run(id mgr, id dev, BOOL connect) {
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block NSError *out = nil;
    void (^done)(NSError *) = ^(NSError *e) { out = e; dispatch_semaphore_signal(sem); };
    if (connect) [mgr connectToDevice:dev completion:done];
    else [mgr disconnectFromDevice:dev completion:done];
    if (dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)))
        return [NSError errorWithDomain:@"sidecar" code:1 userInfo:@{NSLocalizedDescriptionKey: @"timed out"}];
    return out;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        setvbuf(stdout, NULL, _IOLBF, 0);
        Class cls = dlopen("/System/Library/PrivateFrameworks/SidecarCore.framework/SidecarCore", RTLD_LAZY)
                        ? NSClassFromString(@"SidecarDisplayManager") : nil;
        id mgr = [cls sharedManager];
        if (!mgr) { fprintf(stderr, "SidecarCore is not available on this macOS\n"); return 2; }
        NSMutableArray<NSString *> *args = [NSMutableArray array];
        for (int i = 1; i < argc; i++) [args addObject:@(argv[i])];
        NSString *cmd = args.count ? args[0] : @"list";
        if (args.count) [args removeObjectAtIndex:0];
        double wait = 0;
        NSUInteger w = [args indexOfObject:@"--wait"];
        if (w != NSNotFound && w + 1 < args.count) {
            wait = [args[w + 1] doubleValue];
            [args removeObjectsInRange:NSMakeRange(w, 2)];
        }
        NSString *want = args.firstObject;
        BOOL (^match)(id) = ^BOOL(id d) { return !want || [[d name] isEqualToString:want]; };

        if ([cmd isEqualToString:@"list"]) {
            NSArray *on = [[mgr connectedDevices] valueForKey:@"name"];
            for (id d in [mgr devices]) printf("%s %s\n", [on containsObject:[d name]] ? "*" : " ", [[d name] UTF8String]);
            return 0;
        }
        if ([cmd isEqualToString:@"connect"]) {
            NSDate *until = [NSDate dateWithTimeIntervalSinceNow:wait];
            NSString *last = @"no iPad found";
            for (;;) {
                for (id d in [mgr connectedDevices]) if (match(d)) { printf("already connected\n"); return 0; }
                for (id d in [mgr devices]) if (match(d)) {
                    NSError *e = run(mgr, d, YES);
                    if (!e) { printf("connected to %s\n", [[d name] UTF8String]); return 0; }
                    last = e.localizedDescription;
                    break;
                }
                if ([until timeIntervalSinceNow] <= 0) break;
                [NSThread sleepForTimeInterval:10];
            }
            printf("not connected: %s\n", last.UTF8String);
            return 1;
        }
        if ([cmd isEqualToString:@"disconnect"]) {
            for (id d in [mgr connectedDevices]) if (match(d)) {
                NSError *e = run(mgr, d, NO);
                if (e) { printf("failed: %s\n", e.localizedDescription.UTF8String); return 1; }
                printf("disconnected %s\n", [[d name] UTF8String]);
            }
            return 0;
        }
        printf("usage: sidecar list | connect [NAME] [--wait SECONDS] | disconnect [NAME]\n");
        return 2;
    }
}
