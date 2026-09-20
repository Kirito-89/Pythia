class dsu{
    vector<ll> parent,size;
    public:
    dsu(ll n)
    {
        parent.resize(n+1);
        size.resize(n+1);
        for(ll i=0;i<=n;i++)
        {
            parent[i]=i;
            size[i]=1;
        }
    }

    ll find(ll a)
    {
        if(a==parent[a])return a;
        return parent[a]=find(parent[a]);
    }
    void merge(ll a,ll b)
    {
        a=find(a);b=find(b);
        if(a==b)return;
        if(size[a]<size[b])swap(a,b);
        size[a]+=size[b];
        parent[b]=a;
    }
    ll get_parent(ll i)
    {
        return parent[i];
    }
    ll get_size(ll i)
    {
        return size[i];
    }
};
class Solution {

    vector<ll> lp;
    vector<ll> sieve;
    ll n=1e5+2;
public:
    void populate()
    {
        lp.resize(n);
        sieve.resize(n);
        fill(lp.begin(),lp.end(),-1);
        fill(sieve.begin(),sieve.end(),1);
        for(ll i=2;i<n;i++)
        {
            if(sieve[i]==0)continue;
            lp[i]=i;
            for(ll j=i*i;j<n;j+=i)
            {
                sieve[j]=0;
                if(lp[j]==-1)
                {
                    lp[j]=i;
                }
            }
        }
    }

    vector<ll> pfactors(ll n)
    {
        vector<ll> ans;
        while(n>1)
        {
            ll p=lp[n];
            while(n%p==0)
            {
                n/=p;
            }
            ans.push_back(p);
        }
        return ans;
    }
    ll largestComponentSize(vector<ll>& nums) {
        populate();
        map<ll,vector<ll>> mp;
        ll j=0;
        for(auto i:nums)
        {
            vector<ll> factors=pfactors(i);
            for(auto k:factors)mp[k].push_back(j);
            j++;
        }
        dsu d(n);

        for(auto [x,y]:mp)
        {
            ll m=y.size();
            for(ll i=0;i<m-1;i++)
            {
                d.merge(y[i],y[i+1]);
            }
        }
        ll ans=0;
        for(ll i=0;i<n;i++)
        {
            ans=max(ans,d.get_size(i));
        }
        return ans;
    }
};